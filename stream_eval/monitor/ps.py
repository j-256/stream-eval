"""Process discovery and session detection for the dashboard.

Replaces the `ps -eo pid,ppid,etime,cmd` regex parsing in the legacy
single-file monitor with psutil.Process accessors.

Public surface:
- find_eval_workers(): yield dicts describing live trigger/synthesis
  workers ({pid, ppid, kind, skill, eval_path, started_at, cmdline}).
- detect_session(explicit=None): return the Claude Code session id
  the dashboard should pin to, using the layered fallback (explicit ->
  parent bash -> live worker -> recent .output file).
- find_output_files(limit): return the youngest .output paths
  (default last 30 by mtime).
"""
import os
import re
from pathlib import Path

import psutil


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
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline",
                                      "create_time"]):
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
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def find_claude_workers_for(harness_pid):
    """Yield one dict per live `claude -p` child of `harness_pid`.

    Each harness spawns one or more `claude -p` subprocesses (one per
    in-flight fixture run). The dashboard surfaces them under the
    harness's row so the operator sees what's actually executing
    versus just the parent's existence.

    Fields per child:
    - pid: claude subprocess pid
    - started_at: psutil create_time (Unix epoch)
    - cmdline: full argv
    """
    try:
        parent = psutil.Process(harness_pid)
    except psutil.NoSuchProcess:
        return
    try:
        children = parent.children(recursive=False)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    for child in children:
        try:
            if not child.is_running():
                continue
            cmd = child.cmdline()
            if not cmd:
                continue
            # Match argv[0]'s basename exactly. The runner spawns
            # `claude` (PATH-resolved) or an absolute path ending in
            # `claude`. A substring-anywhere-in-argv[0..2] match would
            # false-positive on the runner's own git subprocesses
            # (e.g. `git --git-dir=/x/claude-code-skills/.git ...`),
            # which it shells out to often during worktree management.
            if Path(cmd[0]).name != "claude":
                continue
            yield {
                "pid": child.pid,
                "started_at": child.create_time(),
                "cmdline": cmd,
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


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
    """Return the Claude Code session id the dashboard should pin to.

    Layered fallback:
    1. explicit (from --session): returned as-is.
    2. The dashboard's own bash parent's $CLAUDE_SESSION_ID env var.
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
    """Walk up the parent chain looking for a CLAUDE_SESSION_ID env var.
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
        sid = env.get("CLAUDE_SESSION_ID")
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
    home = Path.home()
    base = home / ".claude" / "projects"
    if not base.is_dir():
        return
    for p in base.rglob("*.output"):
        yield p


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
    """Return the youngest .output paths under ~/.claude/projects.

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
