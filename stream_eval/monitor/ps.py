"""Process discovery and session detection for the dashboard

Replaces the `ps -eo pid,ppid,etime,cmd` regex parsing in the legacy
single-file monitor with psutil.Process accessors.

Public surface:
- find_eval_workers(): yield dicts describing live trigger/synthesis
  workers ({pid, ppid, kind, skill, eval_path, started_at, cmdline}).
- detect_session(explicit=None): return the parent agent session id
  the dashboard should pin to, using the layered fallback (explicit ->
  parent bash -> live worker -> recent .output file).
- find_output_files(limit): return the youngest .output paths
  (default last 30 by mtime).
"""
import json
import os
import re
import subprocess
from pathlib import Path

import psutil

from stream_eval.agents import agent_for_executable
from stream_eval.paths import output_dirs


# Match the harness invocation in cmdline. Three forms:
#   1. Console script: `... stream-eval trigger ...`
#   2. Module form:    `... -m stream_eval.cli trigger ...`
#   3. Legacy in-repo: `... tools/trigger-eval.py ...`
_KIND_TOKEN_RE = re.compile(
    r"\b(?:stream-eval|stream_eval\.cli)\s+(trigger|synthesis)\b"
    r"|tools/(trigger|synthesis)-eval\.py"
)


def find_eval_workers():
    """Yield one dict per live trigger/synthesis worker on this host.

    Fields:
    - pid, ppid: process ids
    - kind: "trigger" or "synthesis"
    - skill: parsed from --skill-path or --skill-name flag, else None
    - eval_path: parsed from --eval flag, else None
    - started_at: psutil create_time (Unix epoch)
    - cmdline: full argv as a list
    """
    # Don't prefetch attrs via process_iter([...]). On macOS, the
    # prefetch path calls proc.cmdline() before our try/except can
    # catch the race where a process exits between enumeration and
    # attribute read -- and the C extension raises SystemError, not
    # NoSuchProcess. Fetch attrs inside the loop body where the
    # except clause covers them.
    try:
        procs = list(psutil.process_iter())
    except (psutil.NoSuchProcess, psutil.AccessDenied, SystemError):
        return
    for proc in procs:
        try:
            if not proc.is_running():
                continue
            cmdline = proc.cmdline()
            if not cmdline:
                continue
            joined = " ".join(cmdline)
            m = _KIND_TOKEN_RE.search(joined)
            if not m:
                continue
            kind = m.group(1) or m.group(2)
            skill = (
                _extract_flag_value(cmdline, "--skill-path")
                or _extract_flag_value(cmdline, "--skill-name")
            )
            eval_path = _extract_flag_value(cmdline, "--eval")
            yield {
                "pid": proc.pid,
                "ppid": proc.ppid(),
                "kind": kind,
                "skill": _basename_or_none(skill),
                "eval_path": eval_path,
                "started_at": proc.create_time(),
                "cmdline": cmdline,
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied,
                SystemError):
            # SystemError here is the macOS-only "process_cmdline
            # raced with proc exit" failure -- treat it the same as
            # the documented races.
            continue


_TRANSCRIPT_PATH_RE = re.compile(r"(\S+/transcripts/[^/]+/[^/]+\.jsonl)\b")
_TRANSCRIPT_FILENAME_RE = re.compile(r"^(?P<fixture_id>.+)-(?P<run>\d+)\.jsonl$")


def find_agent_workers_for(harness_pid):
    """Yield one dict per live agent CLI child of `harness_pid`.

    Each harness spawns one or more agent CLI subprocesses (one per
    in-flight fixture run). The dashboard surfaces them under the
    harness's row so the operator sees what's actually executing
    versus just the parent's existence.

    Each yielded dict carries enough to identify which fixture the
    subprocess is running and how it's faring -- the legacy monitor
    used these for the "X of N in-flight, Y retries" header and the
    pulsing in-flight cells in the segmented bar.

    Fields per child:
    - pid: agent subprocess pid
    - agent: selected agent adapter
    - started_at: psutil create_time (Unix epoch)
    - cmdline: full argv
    - fixture_id: parsed from the transcript filename, or None
    - run: parsed from the transcript filename, or None
    - transcript_path: best-guess path to the worker's open
      transcripts/<out-stem>/<fixture>-<run>.jsonl, or None
    - retries: count of api_retry events seen so far in the
      transcript (0 if no transcript yet -- subprocess just started)
    - latest_attempt: most-recent api_retry attempt counter
    - max_retries_field: max_retries from the latest api_retry event
    - last_error: error string from the latest api_retry event

    Trigger-eval workers don't write to transcripts/ (they use a
    tempfile); for those, fixture_id / run / transcript_path will be
    None even though pid + started_at are correct. That's fine -- the
    dashboard renders "(starting)" for the fixture_id column.

    Sidecar fallback for development: stream_eval.fake writes pre-baked
    worker dicts to a sidecar JSON file when its scenarios include
    in-flight cells. If psutil yields no live children for harness_pid
    (the common case for fake harnesses, which are ledger-only) AND a
    sidecar exists under a stream-eval state directory
    matching this pid, those dicts are yielded. Real harnesses have
    no sidecars and never hit this path.
    """
    real_yielded = False
    try:
        parent = psutil.Process(harness_pid)
        try:
            children = parent.children(recursive=False)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            children = []
    except psutil.NoSuchProcess:
        children = []
    for child in children:
        try:
            if not child.is_running():
                continue
            cmd = child.cmdline()
            if not cmd:
                continue
            agent = agent_for_executable(cmd[0])
            if agent is None:
                continue
            transcript = _resolve_transcript(child)
            stats = _transcript_stats(transcript) if transcript else _empty_stats()
            fixture_id, run = _parse_transcript_filename(transcript)
            real_yielded = True
            yield {
                "pid": child.pid,
                "agent": agent,
                "started_at": child.create_time(),
                "cmdline": cmd,
                "fixture_id": fixture_id,
                "run": run,
                "transcript_path": transcript,
                "retries": stats["total_retries"],
                "latest_attempt": stats["latest_attempt"],
                "max_retries_field": stats["max_retries_field"],
                "last_error": stats["last_error"],
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied,
                SystemError):
            continue
    if not real_yielded:
        yield from _yield_fake_workers_for(harness_pid)


def find_claude_workers_for(harness_pid):
    """Backward-compatible alias for find_agent_workers_for"""
    yield from find_agent_workers_for(harness_pid)


def _yield_fake_workers_for(harness_pid):
    """Read .workers.json sidecars from stream-eval state directories that
    declare in-flight workers for this harness pid. Used by
    stream_eval.fake to inject simulated workers without needing real
    subprocesses; real harnesses don't write these files."""
    for base in output_dirs():
        if not base.is_dir():
            continue
        for path in base.rglob("*.workers.json"):
            try:
                with path.open() as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("harness_pid") != harness_pid:
                continue
            for worker in data.get("workers", []):
                yield worker


def _resolve_transcript(proc):
    """Best-effort: return a path matching */transcripts/<stem>/<f>-<r>.jsonl
    that the process holds open, or None. Tries psutil's open_files()
    first (fast, no shell-out), falls back to lsof on platforms where
    psutil declines to enumerate (macOS sometimes returns []).
    """
    try:
        files = proc.open_files()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    for f in files:
        m = _TRANSCRIPT_PATH_RE.search(f.path)
        if m:
            return m.group(1)
    # psutil.open_files is empty on macOS for some agent processes;
    # fall back to lsof, which the legacy monitor relied on.
    try:
        out = subprocess.run(
            ["lsof", "-p", str(proc.pid)],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode not in (0, 1):
        # lsof returns 1 when some fd lookups failed but at least one
        # line was emitted; that's still useful.
        return None
    candidates = []
    for line in out.stdout.splitlines():
        m = _TRANSCRIPT_PATH_RE.search(line)
        if m:
            candidates.append(m.group(1))
    if not candidates:
        return None
    # Defensive: if the process holds two transcripts open, prefer
    # the youngest (mtime) -- the legacy monitor's heuristic.
    def _mtime(p):
        try:
            return Path(p).stat().st_mtime
        except OSError:
            return 0.0
    candidates.sort(key=_mtime, reverse=True)
    return candidates[0]


def _parse_transcript_filename(transcript_path):
    """Return (fixture_id, run) from a path's basename, or (None, None)."""
    if not transcript_path:
        return (None, None)
    m = _TRANSCRIPT_FILENAME_RE.match(Path(transcript_path).name)
    if not m:
        return (None, None)
    return (m.group("fixture_id"), int(m.group("run")))


def _transcript_stats(path):
    """Count api_retry events + extract the most recent attempt info.

    Returns a dict with total_retries, latest_attempt, max_retries_field,
    last_error. All zeros / None when the path is missing or unreadable
    -- the dashboard tolerates this gracefully (renders the cell as
    in-flight with no retry annotations)."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return _empty_stats()
    total = 0
    latest_attempt = 0
    max_retries = 0
    last_error = None
    for line in text.splitlines():
        if '"api_retry"' not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "system" and d.get("subtype") == "api_retry":
            total += 1
            latest_attempt = d.get("attempt", 0)
            mr = d.get("max_retries", 0)
            if mr > max_retries:
                max_retries = mr
            last_error = d.get("error")
    return {
        "total_retries": total,
        "latest_attempt": latest_attempt,
        "max_retries_field": max_retries,
        "last_error": last_error,
    }


def _empty_stats():
    return {
        "total_retries": 0,
        "latest_attempt": 0,
        "max_retries_field": 0,
        "last_error": None,
    }


def _extract_flag_value(cmdline, flag):
    """Find `<flag> <value>` in cmdline; return value or None."""
    for i, tok in enumerate(cmdline):
        if tok == flag and i + 1 < len(cmdline):
            return cmdline[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return None


def _basename_or_none(p):
    return Path(p).name if p else None


def detect_session(explicit=None):
    """Return the parent agent session id the dashboard should display.

    Layered fallback:
    1. explicit (from --session): returned as-is.
    2. The dashboard's own parent agent session environment.
    3. Any live trigger/synthesis worker's bash-parent session id.
    4. The youngest few .output files' session ids (capped to avoid
       walking thousands of historical files).
    Returns None if no session can be determined.
    """
    if explicit:
        return explicit
    sid = _session_from_parent(os.getpid())
    if sid:
        return sid
    for w in find_eval_workers():
        sid = _session_from_parent(w["pid"])
        if sid:
            return sid
    for path in find_output_files(limit=10):
        sid = _session_from_output_path(path)
        if sid:
            return sid
    return None


def _session_from_parent(pid):
    """Walk up the parent chain looking for an agent session environment.
    Returns the session id, or None."""
    try:
        cur = psutil.Process(pid).parent()
    except psutil.NoSuchProcess:
        return None
    depth = 0
    while cur is not None and depth < 8:
        try:
            env = cur.environ()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            env = {}
        for key in (
            "STREAM_EVAL_SESSION_ID",
            "CLAUDE_SESSION_ID",
            "CODEX_THREAD_ID",
        ):
            sid = env.get(key)
            if sid:
                return sid
        try:
            cur = cur.parent()
        except psutil.NoSuchProcess:
            break
        depth += 1
    return None


def _output_paths():
    """Iterate over .output files used as fallback session-detection input."""
    for base in output_dirs():
        if not base.is_dir():
            continue
        for path in base.rglob("*.output"):
            yield path


def _session_from_output_path(path):
    """Extract session id from an .output file path.

    Returning None here makes detect_session()'s fallback layer 4
    (recent .output file) silently drop through, so the dashboard's
    session pin will only resolve via layers 1-3 (explicit flag,
    parent bash, live worker). This is acceptable: the file-walk
    fallback was a "last-ditch best-effort" in the legacy monitor and
    is rarely the layer that resolves in practice. If the empty
    fallback turns out to matter, decoding the parent dir's encoded
    slug (the legacy approach) goes here.
    """
    return None


def find_output_files(*, limit=None):
    """Return the youngest stream-eval .output paths.

    The legacy 4h time-window model produced surprises: a long-running
    eval on the previous day vanished from the dashboard mid-run, and
    a quiet morning showed nothing despite recent completions. The
    last-N model is more predictable -- we always show the youngest
    `limit` files by mtime, and the per-skill cap is applied later in
    state.build_state so 'active' rows aren't dropped by accident.

    Caveat: a slow active eval whose progress writes are infrequent
    can have an old enough mtime that it falls outside the top-N when
    the operator is running many parallel evals. Bump
    STREAM_EVAL_OUTPUT_LIMIT in that case. A future revision could
    do a two-stage scan (top-N by mtime UNION files whose harness
    pid is alive) to make this guarantee unconditional.

    `limit` defaults to STREAM_EVAL_OUTPUT_LIMIT or 100.
    """
    if limit is None:
        limit = int(os.environ.get("STREAM_EVAL_OUTPUT_LIMIT", "100"))
    paths = []
    for p in _output_paths():
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        paths.append((mtime, p))
    paths.sort(key=lambda pair: -pair[0])
    if limit is not None:
        paths = paths[:limit]
    return [p for _mt, p in paths]
