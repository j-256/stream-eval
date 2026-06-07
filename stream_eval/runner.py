"""Shared eval-runner library for stream_eval.trigger and stream_eval.synthesis.

Owns: Dispatcher-based dispatch, abort-on-first-timeout, the canonical
stderr progress line, the startup banner, the results-JSON envelope,
fixture-id assignment with collision detection, worktree-isolation
detect+restore around each spawn.

Does NOT own: fixture schemas, scoring (trigger vs. assertion), per-kind
defaults, transcript JSONL persistence (synthesis-only behavior toggled
by the harness passing transcript_dir=Path).

Each harness imports run_eval and supplies kind-specific callbacks
(see stream_eval/trigger.py and stream_eval/synthesis.py for examples).
"""
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from stream_eval.pool import Dispatcher

from stream_eval.env import load_dotenv
from stream_eval.isolation import prepare_isolated_home
from stream_eval.subprocess import run_with_retry_aware_bail

load_dotenv()
EVAL_MODEL = os.environ.get("STREAM_EVAL_MODEL", "sonnet")

# Set during run_eval; signal handlers and socket listeners use this
# to adjust the running dispatcher's target_workers / state. None when
# no eval is in flight.
_CURRENT_DISPATCHER = None


def get_current_dispatcher():
    """Return the running Dispatcher, or None if no eval is in flight.

    Used by stream_eval.control's signal handlers and socket listener
    to bind their commands to the current run."""
    return _CURRENT_DISPATCHER


def _resolve_harness_version(package_dir=None):
    """Identify this harness's version for the results envelope.

    Lookup order:
    1. If a .git directory exists at package_dir/.. (e.g. submodule
       checkout, dev install), read .git/HEAD. If it's a `ref: ...`
       reference, resolve to the actual SHA via .git/refs/.... If it's
       a SHA directly (detached HEAD), use it. Returns (sha, "git_sha").
    2. Otherwise, return (stream_eval.__version__, "package_version").
    3. If __version__ is missing, return ("unknown", "unknown").

    Any OSError reading inside .git or __init__.py is treated as a
    miss and falls through to the next lookup strategy. This guards
    against restricted-permission CI environments, broken symlinks in
    .git, or transient disk errors -- the version stamp is a
    nice-to-have, not load-bearing, so we'd rather degrade to
    "unknown" than crash run_eval after a successful eval.

    `package_dir` is the directory containing __init__.py; defaults to
    the real stream_eval package's directory. Override in tests.
    """
    if package_dir is None:
        import stream_eval
        package_dir = Path(stream_eval.__file__).resolve().parent
    package_dir = Path(package_dir)

    # Look for a .git in the package's parent (the repo root in dev /
    # submodule scenarios). Submodules use a .git FILE (not a dir)
    # whose contents are `gitdir: <relative-path-to-real-git-dir>`,
    # so we resolve that indirection before reading HEAD.
    git_path = package_dir.parent / ".git"
    git_dir = None
    if git_path.is_dir():
        git_dir = git_path
    elif git_path.is_file():
        try:
            line = git_path.read_text().strip()
        except OSError:
            line = ""
        if line.startswith("gitdir:"):
            target = line[len("gitdir:"):].strip()
            candidate = (git_path.parent / target).resolve()
            if candidate.is_dir():
                git_dir = candidate
    if git_dir is not None:
        head_path = git_dir / "HEAD"
        if head_path.is_file():
            try:
                head = head_path.read_text().strip()
            except OSError:
                head = ""
            if head.startswith("ref:"):
                ref = head[len("ref:"):].strip()
                ref_file = git_dir / ref
                if ref_file.is_file():
                    try:
                        sha = ref_file.read_text().strip()
                    except OSError:
                        sha = ""
                    if sha:
                        return (sha, "git_sha")
                # Fall through to next strategy if ref unresolvable.
            elif head:
                # Detached HEAD; HEAD itself is the SHA.
                return (head, "git_sha")

    # Fallback: package __version__.
    init = package_dir / "__init__.py"
    if init.is_file():
        try:
            text = init.read_text()
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("__version__"):
                # Tolerate single or double quotes.
                _, _, val = line.partition("=")
                val = val.strip().strip("'").strip('"')
                if val:
                    return (val, "package_version")

    return ("unknown", "unknown")


# MCP and Agent strip flags. Used by both `isolated` and `restricted`
# profiles below; pulled out to a constant so the two stay in sync if the
# strip set ever changes.
_STRIP_MCP_AGENT_FLAGS = [
    "--strict-mcp-config",
    "--mcp-config", '{"mcpServers":{}}',
    "--disallowedTools", "Agent",
]

# Three profiles, semantically distinct:
# - isolated (default): temp HOME containing ONLY the skill under test;
#   --strict-mcp-config and --disallowedTools Agent strip MCP/Agent.
#   Production-equivalent for a vanilla install.
# - restricted: user's real HOME; same MCP/Agent strip flags. Tests a
#   skill against the user's other globally-installed skills but without
#   MCP/Agent.
# - inherit: user's real HOME; no flags stripped. Closest to interactive
#   use; useful for diagnostic runs.
PROFILE_FLAGS = {
    "isolated": _STRIP_MCP_AGENT_FLAGS,
    "restricted": _STRIP_MCP_AGENT_FLAGS,
    "inherit": [],
}


class FixtureSchemaError(Exception):
    pass


def assign_fixture_ids(fixtures, get_name):
    """Return [(fixture_id, fixture)] in input order.

    fixture_id = get_name(fixture) if it returns a non-empty string,
    else the lowest-unused 'qN' slot. Raises FixtureSchemaError on
    duplicate explicit names.
    """
    explicit = []
    explicit_set = set()
    for fx in fixtures:
        name = get_name(fx)
        if isinstance(name, str) and name:
            if name in explicit_set:
                raise FixtureSchemaError(f"duplicate fixture name: {name!r}")
            explicit_set.add(name)
            explicit.append(name)
        else:
            explicit.append(None)
    result = []
    next_idx = 0
    for fx, name in zip(fixtures, explicit):
        if name is not None:
            result.append((name, fx))
            continue
        while f"q{next_idx}" in explicit_set:
            next_idx += 1
        fid = f"q{next_idx}"
        result.append((fid, fx))
        explicit_set.add(fid)
        next_idx += 1
    return result


QUERY_DISPLAY_MAX = 80


PROGRESS_LINE_RE = re.compile(
    r"\[(?P<n>\d+)/(?P<total>\d+)\]\s+"
    r"kind=(?P<kind>trigger|synthesis)\s+"
    r"pass=(?P<pass_>True|False)\s+"
    r"fixture_id=(?P<fixture_id>\S+)\s+"
    r"run=(?P<run>\d+)\s+"
    r"elapsed=(?P<elapsed>[\d.]+)s\s+"
    r"retries=(?P<retries>\d+)\s+"
    r"timeout_reason=(?P<timeout_reason>none|retry_budget|wall_clock_in_retry|wall_clock)\s+"
    r"first_tool=(?P<first_tool>\S+)\s+"
    r"first_skill=(?P<first_skill>\S+)\s+"
    r"failed_asserts=(?P<failed_asserts>\d+)"
    # contaminated= is optional so the regex stays byte-identical with
    # the monitor's copy and parses log files written before
    # iteration-eval-harness-worktree-isolation added the field. The
    # runner's emitter ALWAYS includes the field on lines this version
    # produces, so the optional group fires for current-runner output;
    # absence only happens when re-parsing older logs.
    r"(?:\s+contaminated=(?P<contaminated>True|False))?"
    r":\s+(?P<query>.*)$"
)


def _format_progress(*, n, total, kind, pass_, fixture_id, run_idx,
                     elapsed_seconds, total_retries, timeout_reason,
                     first_tool, first_skill, failed_asserts,
                     contaminated, query):
    """Single source of truth for the canonical stderr progress line.

    The monitor parses this with PROGRESS_LINE_RE. Fields are KV-pair
    style for human readability when tailing logs; switching to JSONL
    later is a single function-body change.

    All trailing diagnostic fields (timeout_reason, first_tool,
    first_skill, failed_asserts, contaminated) are required on every
    line. Sentinel values for fields that don't apply to a given kind:
      - timeout_reason="none" when no timeout
      - first_tool="-" / first_skill="-" when no tool was used
      - failed_asserts=0 for trigger runs (which have no assertions)
        and for synthesis runs where every assertion passed
      - contaminated=True iff the spawn left the worktree dirtier than
        it found it (eval-Sonnet edited a tracked source file or
        created a new untracked file). A True value means the run's
        pass verdict is unaudited.
    """
    q_disp = query.replace("\n", " ")[:QUERY_DISPLAY_MAX]
    return (
        f"[{n}/{total}] "
        f"kind={kind} "
        f"pass={pass_} "
        f"fixture_id={fixture_id} "
        f"run={run_idx} "
        f"elapsed={elapsed_seconds}s "
        f"retries={total_retries} "
        f"timeout_reason={timeout_reason} "
        f"first_tool={first_tool} "
        f"first_skill={first_skill} "
        f"failed_asserts={failed_asserts} "
        f"contaminated={contaminated}"
        f": {q_disp}"
    )


STARTUP_BANNER_RE = re.compile(
    r"^\s*=== eval starting: "
    r"kind=(?P<kind>trigger|synthesis)\s+"
    r"skill=(?P<skill>\S+)\s+"
    r"eval=(?P<eval>\S+)\s+"
    r"runs=(?P<runs>\d+)\s+"
    r"workers=(?P<workers>\d+)\s+"
    r"total_fixtures=(?P<total_fixtures>\d+)"
    # pid= is optional so the regex still parses banners written before
    # F.5 added per-eval pid routing. New runs always include it.
    r"(?:\s+pid=(?P<pid>\d+))?"
    r"\s*==="
)


# Companion to STARTUP_BANNER_RE: emitted at the end of run_eval. The
# dashboard joins startup -> finish to classify a row as "completed" vs
# "aborted" without needing to peek at results.json paths or guess from
# pid liveness alone (which is racy: a harness can have already exited
# cleanly by the time the dashboard polls).
FINISH_BANNER_RE = re.compile(
    r"^\s*=== eval finished: "
    r"kind=(?P<kind>trigger|synthesis)\s+"
    r"skill=(?P<skill>\S+)\s+"
    r"pid=(?P<pid>\d+)\s+"
    r"verdict=(?P<verdict>completed|aborted)"
    r"\s*==="
)


def format_finish_banner(*, kind, skill, verdict, pid=None):
    """The runner emits this to stderr after the last task finishes.

    verdict is "completed" if every dispatched task scored (pass or
    fail), "aborted" if the harness bailed early (timeout, throttle,
    Ctrl-C). The dashboard pairs this with the startup banner to label
    the row's status; without it, the dashboard would have to rely on
    pid-still-alive, which races against fast harness exits.
    """
    if pid is None:
        pid = os.getpid()
    return (
        f"=== eval finished: "
        f"kind={kind} "
        f"skill={skill} "
        f"pid={pid} "
        f"verdict={verdict} ==="
    )


def format_startup_banner(*, kind, skill, eval_path, runs, workers,
                          total_fixtures, pid=None):
    """The runner emits this to stderr before the first task completes.

    stream_eval.monitor parses it from each .output file to bind finished
    runs to (skill, kind, harness_pid). total_fixtures lets the dashboard
    render an authoritative qpass denominator from the start of the run,
    before any rows have arrived. pid identifies the harness process so
    each row's worker-control buttons route to its own /tmp/stream-eval-
    <pid>.sock; defaults to os.getpid() so callers don't have to pass it.
    """
    if pid is None:
        pid = os.getpid()
    return (
        f"=== eval starting: "
        f"kind={kind} "
        f"skill={skill} "
        f"eval={eval_path} "
        f"runs={runs} "
        f"workers={workers} "
        f"total_fixtures={total_fixtures} "
        f"pid={pid} ==="
    )


def _install_stderr_tee():
    """Replace sys.stderr with a tee that forwards to the original
    stderr AND to ~/.claude/projects/stream-eval/<harness-pid>.output.

    The dashboard's find_output_files walks ~/.claude/projects/ for
    *.output files. Without this tee, real evals are invisible to the
    dashboard because Claude Code's Bash tool puts background-process
    stderr under /tmp/claude-501/, which the dashboard doesn't scan.

    Idempotent: a second call is a no-op (we set a flag on the wrapped
    stream). Failures are silently swallowed because dashboard
    visibility is a UX feature, not a correctness one -- if HOME is
    weird or the projects dir isn't writable, the eval should still
    run.
    """
    if getattr(sys.stderr, "_stream_eval_teed", False):
        return
    try:
        log_dir = Path.home() / ".claude" / "projects" / "stream-eval"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{os.getpid()}.output"
        log_file = open(log_path, "w", buffering=1)  # line-buffered
    except OSError:
        return
    original = sys.stderr
    sys.stderr = _StderrTee(original, log_file)


class _StderrTee:
    """Forwards .write/.flush to two underlying streams. Anything else
    is delegated to the original stream so the file-like contract
    (encoding, isatty, fileno on the real one only, etc.) stays
    intact for callers that introspect stderr."""
    _stream_eval_teed = True

    def __init__(self, original, log_file):
        self._original = original
        self._log_file = log_file

    def write(self, data):
        n = self._original.write(data)
        try:
            self._log_file.write(data)
        except (OSError, ValueError):
            # Disk full / file closed -- keep the original stderr
            # working so the eval doesn't abort over a logging issue.
            pass
        return n

    def flush(self):
        try:
            self._original.flush()
        except OSError:
            pass
        try:
            self._log_file.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self._original, name)


def _git_dirty_set(cwd):
    """Return the set of repo-relative paths git considers dirty in `cwd`
    (modified, added, deleted, renamed, untracked-not-gitignored).

    Uses `git status --porcelain=v1 -z` for unambiguous parsing: NUL
    separators tolerate spaces and renames in path names. Each record
    is `XY <path>` where XY is the two-character status code; rename
    records (`R <to>` followed by `<from>` in a separate NUL-delimited
    field) yield both `to` and `from` so a rename from one tracked path
    to another is fully captured by the snapshot.

    Returns paths as POSIX strings relative to the repo root, NOT to
    `cwd`. The two are the same when cwd is the repo root, which is the
    only configuration the harness supports today.

    A non-zero git exit -- a non-git directory, a corrupt index, a
    permissions error -- raises CalledProcessError. The caller treats
    that as fatal: an eval whose worktree status can't be observed
    cannot honestly claim "no contamination."
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=cwd, capture_output=True, check=True, text=False,
    )
    out = proc.stdout
    paths = set()
    i = 0
    while i < len(out):
        # Find the next NUL.
        j = out.find(b"\x00", i)
        if j == -1:
            break
        record = out[i:j]
        i = j + 1
        if len(record) < 3:
            continue
        status = record[:2]
        path = record[3:].decode("utf-8", errors="replace")
        paths.add(path)
        # Rename records: the second NUL-delimited field is the source
        # path. Both ends should be flagged so an `R skills/old skills/new`
        # is detected even if the operator's baseline didn't include
        # either.
        if status[:1] in (b"R", b"C"):
            j2 = out.find(b"\x00", i)
            if j2 == -1:
                break
            src = out[i:j2].decode("utf-8", errors="replace")
            paths.add(src)
            i = j2 + 1
    return paths


def _git_repo_root(cwd):
    """Resolve the repo root containing `cwd`. Raises CalledProcessError
    if cwd is not inside a git work tree."""
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd, capture_output=True, check=True, text=True,
    )
    return proc.stdout.strip()


def _restore_worktree_paths(repo_root, paths):
    """Best-effort restore: `git checkout HEAD --` for tracked paths,
    unlink for newly-appeared untracked paths. Returns the list of paths
    that could NOT be restored (caller surfaces these in the result).

    The two-step shape matters because `git checkout` on an untracked
    path is a no-op (no index entry to restore from), and `unlink` on a
    tracked-but-modified path would silently destroy the operator's
    pristine version. This split honors the actual semantics:
    "contamination delta" = (modifications to tracked) + (newly-created
    untracked) -- each remediated by the matching primitive.

    `paths` is the set of contamination-delta paths returned by
    _diff_dirty_sets; an operator's pre-existing dirty files are NOT in
    that set and so are not touched here.
    """
    failures = []
    if not paths:
        return failures
    # Bucket: tracked-modified vs. newly-untracked. `git ls-files` (no
    # flags) lists the index; a path absent from the index is untracked.
    proc = subprocess.run(
        ["git", "ls-files", "--", *sorted(paths)],
        cwd=repo_root, capture_output=True, check=False, text=True,
    )
    tracked = set(proc.stdout.splitlines()) if proc.returncode == 0 else set()
    untracked = paths - tracked

    if tracked:
        proc = subprocess.run(
            ["git", "checkout", "HEAD", "--", *sorted(tracked)],
            cwd=repo_root, capture_output=True, check=False, text=True,
        )
        if proc.returncode != 0:
            failures.extend(sorted(tracked))

    for rel in sorted(untracked):
        try:
            os.unlink(os.path.join(repo_root, rel))
        except FileNotFoundError:
            # Already gone -- harmless, the contamination self-cleared.
            pass
        except OSError:
            failures.append(rel)
    return failures


def _diff_dirty_sets(before, after):
    """Return paths that became dirty between `before` and `after`
    snapshots. Paths the operator already had dirty before the run
    (their in-flight work) are subtracted: only newly-dirty paths
    count as contamination."""
    return after - before


# Per-spawn worktrees live outside the operator repo so contamination
# inside a worktree can't reach repo-relative content. /tmp gets nuked
# on reboot if cleanup fails for any reason. The path is unique per
# (pid, spawn_id) so parallel workers don't collide.
WORKTREE_ROOT = "/tmp/eval-worktrees"

# Per-worktree-path snapshot of the operator repo's branch set at
# create time. Branches are repo-scoped (shared across worktrees), so
# eval-Sonnet's `git checkout -b feat/phantom` leaks into the operator
# repo's `git branch --list` even though the worktree itself is
# isolated. _destroy_worker_worktree consults this map to decide what
# branches to delete during teardown.
_WORKTREE_BRANCHES_AT_CREATE = {}


def _create_worker_worktree(repo_root, spawn_id):
    """Create a per-spawn `git worktree add` checkout under
    /tmp/eval-worktrees/<pid>-<spawn_id>/. Returns the absolute
    worktree path. Raises subprocess.CalledProcessError on git failure
    so the caller can surface as runner-crashed.

    The worktree starts at HEAD (no `--track`, no branch creation):
    the spawn sees only committed code, not the operator's in-flight
    edits. This is what makes the harness un-self-eatable -- no
    matter what the eval does inside the worktree, the operator's
    uncommitted work on the same files is invisible to it.

    Why a per-spawn worktree (not a shared one): with N parallel
    workers, each spawn needs its own isolated cwd. A shared worktree
    would have the same contamination-leakage problem the v2
    detection-and-restore design hit, just within one worker pool.
    """
    os.makedirs(WORKTREE_ROOT, exist_ok=True)
    wt_path = os.path.join(WORKTREE_ROOT, f"{os.getpid()}-{spawn_id}")
    if os.path.exists(wt_path):
        _destroy_worker_worktree(wt_path)
    subprocess.run(
        ["git", "worktree", "add", "--detach", wt_path, "HEAD"],
        cwd=repo_root, capture_output=True, check=True, text=True,
    )
    # Snapshot the operator repo's current branch set so teardown
    # can detect any phantoms the spawn creates.
    proc = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    )
    _WORKTREE_BRANCHES_AT_CREATE[wt_path] = {
        line for line in proc.stdout.splitlines() if line
    }
    return wt_path


def _destroy_worker_worktree(wt_path):
    """Force-remove the worktree registration, delete any phantom
    branches the spawn left, and trash the directory. Returns the
    list of failure descriptions (empty on success).

    Why phantom-branch cleanup belongs here: branches in git are
    repo-scoped, not worktree-scoped. Eval-Sonnet's `git checkout -b
    feat/phantom` inside the worktree creates a refs/heads/feat/phantom
    in the operator repo's branch list. `git worktree remove` does
    not delete those refs (worktree HEADs that point to a branch
    block worktree removal but the branch itself survives the
    --force removal). We snapshot the branch set when creating the
    worktree (in _create_worker_worktree, via the global
    _WORKTREE_BRANCHES_AT_CREATE map) and delete any branches that
    appeared during the spawn.

    `git worktree remove --force` handles the dirty-worktree case
    (uncommitted changes, files written outside the index). Without
    `--force`, plain `worktree remove` would refuse on a dirty
    worktree and leave a stale registration.

    Trashing the directory after `worktree remove` is belt-and-
    suspenders: if `git worktree remove` succeeded it already deleted
    the dir, so the trash call is a no-op. If git's removal left
    orphan content, trash sweeps it.
    """
    failures = []
    if not os.path.exists(wt_path):
        return failures

    # Snapshot the operator's current branch set before removal.
    # Read-only; safe even if multiple workers run in parallel
    # because the branches we're about to delete are uniquely tied
    # to this worktree's spawn.
    branches_at_create = _WORKTREE_BRANCHES_AT_CREATE.pop(wt_path, None)

    # Resolve the operator repo's git-common-dir from the worktree
    # so we don't have to thread the operator path separately.
    proc = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=wt_path, capture_output=True, text=True, check=False,
    )
    if proc.returncode == 0:
        common_dir = proc.stdout.strip()
        # `git --git-dir` accepts a worktree's common dir; commands
        # then run repo-scoped without needing a checked-out cwd.
        common_args = ["git", f"--git-dir={common_dir}"]
    else:
        common_args = ["git"]

    proc = subprocess.run(
        ["git", "worktree", "remove", "--force", wt_path],
        cwd=wt_path, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        failures.append(f"worktree remove {wt_path}: {proc.stderr.strip()}")

    if os.path.exists(wt_path):
        proc = subprocess.run(
            ["trash", wt_path],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            failures.append(f"trash {wt_path}: {proc.stderr.strip()}")

    # After the worktree is gone, delete any phantom branches that
    # appeared during the spawn. Run against the operator repo via
    # --git-dir; we can't cwd into wt_path because it's been deleted.
    if branches_at_create is not None:
        proc = subprocess.run(
            common_args + ["for-each-ref", "--format=%(refname:short)",
                           "refs/heads/"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            current = {line for line in proc.stdout.splitlines() if line}
            phantoms = current - branches_at_create
            for branch in sorted(phantoms):
                proc = subprocess.run(
                    common_args + ["branch", "-D", branch],
                    capture_output=True, text=True, check=False,
                )
                if proc.returncode != 0:
                    failures.append(
                        f"branch -D {branch}: {proc.stderr.strip()}"
                    )
    return failures


def _spawn_and_bail(query, transcript_path, timeout, cwd,
                    skill_path=None, also_install=()):
    """Run claude -p with the canonical command line in an ephemeral
    per-spawn git worktree. Returns the bail dict from
    run_with_retry_aware_bail with three extra keys:
      - worktree_contaminated (bool): the spawn left the per-spawn
        worktree dirty. The OPERATOR repo is unaffected by definition
        (the spawn never touched it); this flag is for marking runs
        whose pass verdict ran on a contaminated state.
      - worktree_changed_paths (list[str]): repo-relative paths that
        became dirty inside the per-spawn worktree.
      - worktree_restore_failures (list[str]): always empty under the
        worktree-isolation design -- the worktree gets destroyed
        rather than restored. Field retained for envelope-shape
        compatibility with iteration-eval-harness-worktree-isolation.

    Per-spawn worktree isolation: each spawn runs in its own
    `git worktree add` checkout at HEAD, located outside the operator
    repo at /tmp/eval-worktrees/<pid>-<spawn>/. Eval-Sonnet can
    `git checkout -b`, `git submodule add`, edit tracked files, clone
    upstreams -- whatever -- and none of it reaches the operator's
    repo because that's not the cwd it sees. Teardown is unconditional
    `git worktree remove --force`; restore complexity collapses.

    Pivoted to this design after iteration-eval-harness-worktree-
    isolation-v2's detection-and-restore approach proved
    self-destructive: when eval-Sonnet ran `git checkout -b` on the
    operator's repo, our `_restore_branch_state` issued
    `git checkout --force` which discarded all uncommitted edits --
    including the harness source files being edited mid-development.
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    profile = os.environ.get("STREAM_EVAL_PROFILE", "isolated")
    if profile not in PROFILE_FLAGS:
        raise ValueError(
            f"unknown STREAM_EVAL_PROFILE {profile!r}; "
            f"must be one of {sorted(PROFILE_FLAGS)}"
        )
    if profile == "isolated":
        if skill_path is None:
            raise ValueError(
                "profile=isolated requires skill_path; pass --skill-path "
                "to the harness CLI"
            )
        home_ctx = prepare_isolated_home(
            skill_path=skill_path, also_install=also_install,
        )
    else:
        home_ctx = contextlib.nullcontext((None, None))

    cmd = [
        "claude",
        "-p", query,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model", EVAL_MODEL,
        # bypassPermissions: without this, Skill invocations under `claude -p`
        # return is_error: true content="Execute skill: <name>" (the
        # permission-prompt body, fired in non-interactive mode). The model
        # sometimes recovers via a Read fallback on SKILL.md but
        # not deterministically – iteration-harness-skill-load-determinism
        # observed 5/5 passes when SKILL.md loaded vs. freelance from
        # training data when it didn't. Applies globally to both profiles.
        "--permission-mode", "bypassPermissions",
        *PROFILE_FLAGS[profile],
    ]

    repo_root = _git_repo_root(cwd)
    # Spawn id encodes worker identity: pid is in the worktree path,
    # and a uniqifier (timestamp ns) prevents collisions when one
    # worker recycles between spawns.
    spawn_id = f"{int(time.time() * 1e9)}"
    wt_path = _create_worker_worktree(repo_root, spawn_id)
    try:
        with home_ctx as (isolated_home, _isolated_name):
            if isolated_home is not None:
                env["HOME"] = isolated_home
            bail = run_with_retry_aware_bail(
                cmd, transcript_path, env, wt_path, timeout,
            )
        # Detection: did the spawn leave the worktree dirty? The
        # operator repo is untouchable by construction; this flag
        # is purely for marking pass verdicts as unaudited.
        wt_dirty = _git_dirty_set(wt_path)
        if wt_dirty:
            bail["worktree_contaminated"] = True
            bail["worktree_changed_paths"] = sorted(wt_dirty)
        else:
            bail["worktree_contaminated"] = False
            bail["worktree_changed_paths"] = []
        bail["worktree_restore_failures"] = []
    finally:
        teardown_failures = _destroy_worker_worktree(wt_path)
        if teardown_failures:
            # Only stamp if the bail dict already exists (it might
            # not if run_with_retry_aware_bail itself raised). The
            # bigger concern is silent leakage of /tmp/ content.
            try:
                bail["worktree_restore_failures"] = teardown_failures
            except (NameError, UnboundLocalError):
                pass
    return bail


def _run_one_task(fixture, run_idx, fixture_id, transcript_dir,
                  timeout, cwd, get_query, score_run,
                  skill_path=None, also_install=()):
    """Worker entry point: spawn one claude -p, score, return per-run dict.

    transcript_dir=None -> tempfile that gets unlinked. Otherwise the
    transcript is written to <transcript_dir>/<fixture_id>-<run_idx>.jsonl
    and retained.

    Returns a dict with these keys (the actual contract -- consumers
    access via r["fixture_id"], etc.):
      - fixture_id (str): id assigned by assign_fixture_ids
      - run_idx (int): 1-based run index within the fixture
      - elapsed_seconds (float): wall-clock seconds spent in claude -p
      - total_retries (int): retry count from run_with_retry_aware_bail
      - timed_out (bool): True if retry budget or wall clock tripped
      - timeout_reason (str | None): "retry_budget_exhausted",
        "wall_clock", or None
      - transcript_path (str | None): persisted path when transcript_dir
        was supplied, else None (tempfile already unlinked)
      - pass_ (bool): score_run's pass verdict; forced False on timeout
        regardless of what scoring returned
      - kind_extra (dict): score_run's free-form per-run payload. Now
        populated even on timeouts so first_tool / first_skill /
        assertion_results survive partial runs; empty only if score_run
        raised.
      - worktree_contaminated (bool): True if the spawn left the
        worktree dirtier than it found it (eval-Sonnet edited source).
        A contaminated run's pass_ is unaudited regardless of value --
        per-run scoring runs on the contaminated state, not on HEAD.
      - worktree_changed_paths (list[str]): repo-relative paths that
        became dirty during the spawn (post-baseline-subtraction).
      - worktree_restore_failures (list[str]): paths auto-restore
        couldn't revert. Operator must clean by hand if non-empty.
    """
    query = get_query(fixture)
    if transcript_dir is None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            transcript_path = f.name
        retain = False
    else:
        Path(transcript_dir).mkdir(parents=True, exist_ok=True)
        transcript_path = str(
            Path(transcript_dir) / f"{fixture_id}-{run_idx}.jsonl"
        )
        retain = True

    t0 = time.time()
    try:
        bail = _spawn_and_bail(query, transcript_path, timeout, cwd,
                              skill_path=skill_path, also_install=also_install)
        elapsed = round(time.time() - t0, 2)
        timed_out = (bail["retry_budget_exhausted"]
                     or bail["wall_timed_out"]
                     or bail.get("wall_timed_out_in_retry", False))
        if bail["retry_budget_exhausted"]:
            timeout_reason = "retry_budget_exhausted"
        elif bail["wall_timed_out"]:
            timeout_reason = "wall_clock"
        elif bail.get("wall_timed_out_in_retry"):
            timeout_reason = "wall_clock_in_retry"
        else:
            timeout_reason = None

        # Score even on timed-out runs so kind_extra (first_tool,
        # first_skill, assertion_results) is preserved -- a timeout tells
        # us about runtime, not about whether the skill triggered or what
        # the partial transcript shows. The run still counts as a failure
        # (pass_ is forced False below), but the trigger/synthesis signal
        # is recoverable from kind_extra without re-reading transcripts.
        # Scorers must tolerate transcripts that may be truncated mid-run
        # (no final result event, partial tool_use chain).
        try:
            pass_, kind_extra = score_run(fixture, transcript_path, bail)
        except Exception:
            pass_, kind_extra = False, {}
        if timed_out:
            pass_ = False

        return {
            "fixture_id": fixture_id,
            "run_idx": run_idx,
            "elapsed_seconds": elapsed,
            "total_retries": bail.get("total_retries", 0),
            "timed_out": timed_out,
            "timeout_reason": timeout_reason,
            "transcript_path": transcript_path if retain else None,
            "pass_": pass_,
            "kind_extra": kind_extra,
            "worktree_contaminated": bail.get("worktree_contaminated", False),
            "worktree_changed_paths": bail.get("worktree_changed_paths", []),
            "worktree_restore_failures": bail.get(
                "worktree_restore_failures", []
            ),
        }
    finally:
        if not retain:
            try:
                os.unlink(transcript_path)
            except Exception:
                pass


def _pool_target(args_tuple):
    """Legacy pickle target retained for reference. Not called by the
    Dispatcher-based dispatch path; kept so any external callers that
    reference it by name do not break on import."""
    (fixture, run_idx, fixture_id, transcript_dir, timeout, cwd,
     get_query, score_run, skill_path, also_install) = args_tuple
    return _run_one_task(fixture, run_idx, fixture_id,
                          transcript_dir, timeout, cwd,
                          get_query, score_run,
                          skill_path=skill_path, also_install=also_install)


class _SubprocessWorker:
    """Adapter from _run_one_task's call signature to the WorkerSlot
    contract Dispatcher expects (start, is_done, join, result).

    Runs _run_one_task in a daemon thread; the actual `claude -p`
    subprocess is spawned inside that thread (so the thread blocks on
    Popen.wait, not on a process-pool boundary). One thread per
    in-flight subprocess; thread overhead is negligible compared to
    the multi-second `claude -p` runtime.
    """
    __slots__ = ("_task", "_kwargs", "_thread", "_done", "result")

    def __init__(self, task, kwargs):
        self._task = task
        self._kwargs = kwargs
        self._thread = None
        self._done = threading.Event()
        self.result = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self.result = _run_one_task(**self._kwargs)
        except Exception as exc:
            # Swallowing here would silently drop the task; surface as
            # a failure record the harness can score.
            self.result = {
                "fixture_id": self._kwargs.get("fixture_id"),
                "run_idx": self._kwargs.get("run_idx"),
                "elapsed_seconds": 0,
                "total_retries": 0,
                "timed_out": False,
                "timeout_reason": None,
                "transcript_path": None,
                "pass_": False,
                "kind_extra": {"error": repr(exc)},
                "worktree_contaminated": False,
                "worktree_changed_paths": [],
                "worktree_restore_failures": [],
            }
        finally:
            self._done.set()

    def is_done(self):
        return self._done.is_set()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout=timeout)


def run_eval(*, kind, fixtures, get_fixture_id, get_query, score_run,
             summarize, runs_per_fixture, workers, timeout, cwd,
             transcript_dir, summary_label,
             skill_name, eval_path,
             skill_path=None, also_install=(),
             executor_class=None):
    """Drive the eval. Returns (results_dict, exit_code).

    Caller writes the JSON and propagates exit code.

    Exit codes:
      0 -- all fixtures pass
      1 -- at least one fixture fails
      3 -- aborted on retry-budget exhaustion or wall-clock timeout

    The `summarize` callback receives a list of
    {"fixture_id": str, "fixture": dict, "runs": list[dict]}
    items and must return a list of dicts each carrying a "pass" key.
    The runner counts `not item["pass"]` to determine the success/fail
    exit code, so harnesses MUST include "pass" on every summary item.

    Other kwargs:
      - kind: "trigger" | "synthesis". Emitted on the canonical stderr
        line and in the envelope's `kind` field; the monitor uses it to
        color/label rows.
      - fixtures: list of dicts; opaque to the runner. Length determines
        `total_fixtures` in the envelope and the banner.
      - get_fixture_id: callable (fixture) -> str | None. Non-empty
        string is the explicit id; anything else triggers fallback to
        `qN`. Passed to assign_fixture_ids as `get_name`.
      - get_query: callable (fixture) -> str. The query sent to
        claude -p, also used to format the human-readable tail of each
        progress line.
      - score_run: callable (fixture, transcript_path, bail) ->
        (pass: bool, kind_extra: dict). Runs only on non-timed-out
        completions (timed-out runs auto-fail with empty kind_extra).
        The bail dict comes from run_with_retry_aware_bail. kind_extra
        is the harness's free-form per-run payload; the runner extracts
        `first_tool`, `first_skill`, and `assertion_results` for the
        canonical line if present.
      - runs_per_fixture: int. Total tasks dispatched =
        len(fixtures) * runs_per_fixture.
      - cwd: str. Passed to claude -p subprocesses as their working
        directory.
      - transcript_dir: Path | None. None means each run's transcript
        goes to a tempfile that's unlinked after scoring (trigger-eval
        default); a Path means transcripts persist at
        <transcript_dir>/<fixture_id>-<run_idx>.jsonl for offline
        debugging (synthesis-eval default).
      - summary_label: str. "queries" or "fixtures" -- appears in the
        closing summary line ("=== {kind}-eval: N/M {summary_label}
        passed (..) ===").
      - skill_name: str. Emitted on the startup banner; the monitor
        uses it to bind .output files to (skill, kind) for finished
        runs.
      - eval_path: str. Path to the fixture JSON file; emitted on the
        startup banner and stored in the envelope's `eval_set` field
        for downstream tooling.

    executor_class is accepted but ignored. Retained for backward
    compatibility with callers that were written when the dispatch path
    used concurrent.futures; the Dispatcher-based path does not need it.
    """
    # Force line-buffered stderr so each progress line flushes
    # immediately. When the harness runs under a Bash tool's
    # `run_in_background` (or any pipe redirection) Python defaults
    # stderr to fully-buffered. The dashboard polls the .output file
    # every 5s -- without this, the file stays empty until the eval
    # finishes and the buffer flushes, so the dashboard reports
    # 0/total throughout the run.
    try:
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        # reconfigure is Python 3.7+ on text streams; if it's not
        # available (or stderr is something exotic), continue with
        # default buffering rather than abort the eval.
        pass

    # Tee stderr to a known location under ~/.claude/projects/ so the
    # dashboard's file walk discovers this run without the operator
    # having to redirect stderr by hand. Claude Code's Bash tool used
    # to put background-process stderr under ~/.claude/projects/...
    # /<id>.output, which the legacy monitor scanned; that path moved
    # to /tmp/claude-501/ in recent versions, so the dashboard now
    # sees nothing from real evals unless we write our own copy.
    _install_stderr_tee()
    # Print the startup banner BEFORE assigning ids -- if assignment
    # raises, the harness still gets a banner-less abort, which is fine.
    print(
        format_startup_banner(
            kind=kind, skill=skill_name, eval_path=eval_path,
            runs=runs_per_fixture, workers=workers,
            total_fixtures=len(fixtures),
        ),
        file=sys.stderr,
    )

    id_pairs = assign_fixture_ids(fixtures, get_fixture_id)

    # Round-robin by run (run-major), not by fixture (fixture-major). With
    # partial coverage -- the upstream API throttles, the harness aborts,
    # the user Ctrl-Cs -- round-robin guarantees every fixture gets at
    # least one run before any fixture gets a second. Fixture-major
    # ordering would leave declines at the tail of the corpus with 0
    # measurements while front-loaded fixtures got the full N runs.
    tasks = []
    for run_idx in range(1, runs_per_fixture + 1):
        for fixture_id, fixture in id_pairs:
            tasks.append((
                fixture, run_idx, fixture_id,
                str(transcript_dir) if transcript_dir else None,
                timeout, cwd, get_query, score_run,
                skill_path, also_install,
            ))

    results_by_id = {fid: {"fixture": fx, "runs": []}
                      for fid, fx in id_pairs}
    # Build kwargs dicts for each task so _SubprocessWorker can call
    # _run_one_task(**kwargs) without unpacking a positional tuple.
    task_kwargs_list = []
    for (fx, run_idx, fixture_id, td, to, task_cwd,
         gq, sr, sp, ai) in tasks:
        task_kwargs_list.append({
            "fixture": fx,
            "run_idx": run_idx,
            "fixture_id": fixture_id,
            "transcript_dir": td,
            "timeout": to,
            "cwd": task_cwd,
            "get_query": gq,
            "score_run": sr,
            "skill_path": sp,
            "also_install": ai,
        })

    total = len(task_kwargs_list)
    t0 = time.time()
    done = 0
    aborted_on_timeout = False
    # Tasks cancelled by per-fixture wall-clock skip. Bumped into `done`
    # so the loop terminates without these ever running -- they're not
    # "missing data", they're "skipped because their fixture made the
    # model think too long once already." Tracked separately for the
    # final summary.
    skipped_for_fixture_timeout = 0
    skipped_fixtures = set()

    def _spawn(kwargs):
        return _SubprocessWorker(task=kwargs, kwargs=kwargs)

    dispatcher = Dispatcher(target_workers=workers, spawn_worker=_spawn)
    for kwargs in task_kwargs_list:
        dispatcher.submit(kwargs)

    # Stash the dispatcher at module level so control surfaces (added in
    # D6) can adjust it from outside. Reset to None in the finally block.
    global _CURRENT_DISPATCHER
    _CURRENT_DISPATCHER = dispatcher

    def _drive_dispatcher():
        # timeout=None means run_until_complete cannot raise TimeoutError;
        # any other exception is a real bug we want to surface, not swallow.
        # The thread is daemon=True so an uncaught exception prints the
        # traceback to stderr without aborting the main thread.
        dispatcher.run_until_complete(timeout=None)

    driver = threading.Thread(target=_drive_dispatcher, daemon=True)
    driver.start()

    try:
        while done < total:
            new_records = list(dispatcher.drain_completed())
            if not new_records:
                time.sleep(0.05)
                continue
            for r in new_records:
                # Look up the fixture for the query display.
                fx = results_by_id[r["fixture_id"]]["fixture"]
                fixture_id = r["fixture_id"]
                run_idx = r["run_idx"]

                results_by_id[fixture_id]["runs"].append(r)
                done += 1

                # Map runner-internal timeout_reason to the on-line
                # vocabulary. Internal values: "retry_budget_exhausted",
                # "wall_clock", "wall_clock_in_retry", None. Line
                # values: "retry_budget", "wall_clock",
                # "wall_clock_in_retry", "none".
                tr_internal = r.get("timeout_reason")
                if tr_internal == "retry_budget_exhausted":
                    tr_line = "retry_budget"
                elif tr_internal == "wall_clock":
                    tr_line = "wall_clock"
                elif tr_internal == "wall_clock_in_retry":
                    tr_line = "wall_clock_in_retry"
                else:
                    tr_line = "none"

                kx = r.get("kind_extra") or {}
                print(
                    _format_progress(
                        n=done, total=total, kind=kind,
                        pass_=r["pass_"],
                        fixture_id=r["fixture_id"],
                        run_idx=r["run_idx"],
                        elapsed_seconds=r["elapsed_seconds"],
                        total_retries=r["total_retries"],
                        timeout_reason=tr_line,
                        first_tool=kx.get("first_tool") or "-",
                        first_skill=kx.get("first_skill") or "-",
                        failed_asserts=sum(
                            1 for ar in kx.get("assertion_results") or []
                            if not ar.get("pass", False)
                        ),
                        contaminated=r.get("worktree_contaminated", False),
                        query=get_query(fx),
                    ),
                    file=sys.stderr,
                )

                if r.get("worktree_contaminated"):
                    changed = r.get("worktree_changed_paths") or []
                    failures = r.get("worktree_restore_failures") or []
                    msg = (
                        f"  ! WORKTREE CONTAMINATED on "
                        f"{r['fixture_id']}-{r['run_idx']}: "
                        f"{len(changed)} path(s) changed -- "
                        f"{', '.join(changed[:5])}"
                        + (
                            f" (+{len(changed) - 5} more)"
                            if len(changed) > 5 else ""
                        )
                    )
                    if failures:
                        msg += (
                            f"; worktree teardown FAILED on "
                            f"{', '.join(failures)} (clean by hand)"
                        )
                    else:
                        msg += "; worktree destroyed (operator repo untouched)"
                    print(msg, file=sys.stderr)

                if r["timed_out"]:
                    # Two kinds of timeout, two policies:
                    #
                    # `retry_budget_exhausted` and `wall_clock_in_retry`
                    # are upstream-poisoned signals -- the CLI either
                    # blew through its retry ceiling or got wedged inside
                    # a retry sleep. Continuing past these mixes real
                    # failures with throttle noise; abort the whole eval.
                    #
                    # `wall_clock` (without retry) is "the model thinks
                    # too long on this prompt." Not a throttle signal; a
                    # specific prompt's pathology. Skip the fixture's
                    # remaining runs (they'd likely time out the same
                    # way) and continue on other fixtures.
                    upstream_poisoned = r["timeout_reason"] in (
                        "retry_budget_exhausted",
                        "wall_clock_in_retry",
                    )
                    if upstream_poisoned:
                        aborted_on_timeout = True
                        cause = (
                            "CLI's retry budget exhausted "
                            "(upstream-poisoned signal)"
                            if r["timeout_reason"] == "retry_budget_exhausted"
                            else "wall clock exceeded inside retry-backoff "
                            "(stuck-during-retry, likely throttle)"
                        )
                        remaining = total - done
                        print(
                            f"\n=== ABORT: run {fixture_id}-{run_idx} "
                            f"timed out -- {cause}. Cancelling remaining "
                            f"{remaining} runs. Continuing measurements "
                            "after a budget-exhaustion event would mix "
                            "real failures with throttle noise. Re-run "
                            "when the upstream API has recovered.",
                            file=sys.stderr,
                        )
                        # Stop the dispatcher cleanly: target_workers=0
                        # blocks any further spawns, and stop()
                        # transitions to STOPPED so the driver thread's
                        # run_until_complete loop exits on its next
                        # poll. pause() alone wouldn't do this.
                        dispatcher.target_workers = 0
                        dispatcher.stop()
                        break
                    # Per-fixture wall-clock skip. Cancel pending runs
                    # for the same fixture_id; log the count.
                    cancelled = dispatcher.cancel_pending(
                        lambda task, fid=fixture_id:
                            task.get("fixture_id") == fid
                    )
                    if cancelled > 0:
                        skipped_for_fixture_timeout += cancelled
                        skipped_fixtures.add(fixture_id)
                        # Bump done by cancelled count so the
                        # `while done < total` loop terminates after
                        # the surviving tasks finish.
                        done += cancelled
                        print(
                            f"  ! WALL-CLOCK on {fixture_id}-{run_idx}: "
                            f"skipping {cancelled} remaining run(s) for "
                            "this fixture; eval continues",
                            file=sys.stderr,
                        )

            if aborted_on_timeout:
                break
    finally:
        _CURRENT_DISPATCHER = None

    driver.join(timeout=10)

    elapsed = time.time() - t0

    fixtures_with_runs = [
        {"fixture_id": fid, "fixture": entry["fixture"],
         "runs": entry["runs"]}
        for fid, entry in results_by_id.items()
    ]
    summary = summarize(fixtures_with_runs)

    contaminated_runs = sum(
        1 for entry in results_by_id.values()
        for r in entry["runs"]
        if r.get("worktree_contaminated")
    )

    envelope = {
        "kind": kind,
        "eval_set": eval_path,
        "runs_per_fixture": runs_per_fixture,
        "total_fixtures": len(fixtures),
        "elapsed_seconds": round(elapsed, 1),
        "aborted_on_timeout": aborted_on_timeout,
        # `completed_runs` counts both genuine completions and per-
        # fixture skips (fixtures whose first run wall-clock-timed-out
        # have their remaining runs cancelled). The `skipped_*` fields
        # below break that down so consumers can tell the difference.
        "completed_runs": done,
        "total_runs_planned": total,
        "skipped_runs_for_fixture_timeout": skipped_for_fixture_timeout,
        "skipped_fixtures": sorted(skipped_fixtures),
        "contaminated_runs": contaminated_runs,
        "results": summary,
    }

    harness_version, harness_version_kind = _resolve_harness_version()
    envelope["harness_version"] = harness_version
    envelope["harness_version_kind"] = harness_version_kind

    verdict = "aborted" if aborted_on_timeout else "completed"
    print(
        format_finish_banner(kind=kind, skill=skill_name, verdict=verdict),
        file=sys.stderr,
    )

    if aborted_on_timeout:
        return envelope, 3

    fixtures_failed = sum(1 for r in summary if not r.get("pass", False))
    closing = (
        f"\n=== {kind}-eval: {len(summary) - fixtures_failed}"
        f"/{len(summary)} {summary_label} passed "
        f"({elapsed:.1f}s) ==="
    )
    print(closing, file=sys.stderr)

    return envelope, (0 if fixtures_failed == 0 else 1)
