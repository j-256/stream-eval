"""Parser for the runner's canonical stderr line and startup banner.

The runner emits these (centralized in stream_eval.runner._format_progress
and stream_eval.runner.format_startup_banner). Both this module and
runner.py import the regex constants from a common point so they stay
in sync.

This is the parser side of the contract. The runner-side emitter lives
in runner.py; tests exercise both ends.
"""
from stream_eval.runner import (
    FINISH_BANNER_RE as _FINISH_BANNER_RE,
    PROGRESS_LINE_RE as _PROGRESS_LINE_RE,
    STARTUP_BANNER_RE as _STARTUP_BANNER_RE,
)
from stream_eval.agents import DEFAULT_AGENT


def parse_progress_line(line):
    """Return a dict of the parsed fields, or None if the line isn't a
    progress line.

    Fields: n, total, kind, pass_, fixture_id, run, elapsed, retries,
    timeout_reason, first_tool, first_skill, failed_asserts,
    contaminated, query. Numeric fields are coerced; pass_ and
    contaminated are booleans.
    """
    m = _PROGRESS_LINE_RE.match(line.strip())
    if not m:
        return None
    g = m.groupdict()
    return {
        "n": int(g["n"]),
        "total": int(g["total"]),
        "kind": g["kind"],
        "pass_": g["pass_"] == "True",
        "fixture_id": g["fixture_id"],
        "run": int(g["run"]),
        "elapsed": float(g["elapsed"]),
        "retries": int(g["retries"]),
        "timeout_reason": g["timeout_reason"],
        "first_tool": g["first_tool"],
        "first_skill": g["first_skill"],
        "failed_asserts": int(g["failed_asserts"]),
        "contaminated": (g.get("contaminated") == "True"),
        "query": g["query"],
    }


def parse_startup_banner(line):
    """Return a dict of the parsed fields, or None.

    The `pid` field is None for legacy banners written before F.5 added
    per-eval pid routing; the dashboard treats those rows as 'unknown'
    status (no live controls).

    `started_at` is None for legacy banners that pre-date the timestamp
    field. New banners always carry it. Float seconds since the unix
    epoch.
    """
    m = _STARTUP_BANNER_RE.search(line)
    if not m:
        return None
    g = m.groupdict()
    pid_str = g.get("pid")
    started_at_str = g.get("started_at")
    return {
        "kind": g["kind"],
        "agent": g.get("agent") or DEFAULT_AGENT,
        "skill": g["skill"],
        "eval": g["eval"],
        "runs": int(g["runs"]),
        "workers": int(g["workers"]),
        "total_fixtures": int(g["total_fixtures"]),
        "pid": int(pid_str) if pid_str else None,
        "started_at": float(started_at_str) if started_at_str else None,
    }


def parse_finish_banner(line):
    """Return a dict of the parsed fields, or None.

    Emitted by the runner at end of run_eval. The dashboard joins it
    with the startup banner (matched by pid) to set DashboardRow.status
    to 'completed' or 'aborted'. Older .output files written before
    F.5 introduced this banner won't have one; rows from those files
    fall through to liveness-based status inference.

    `finished_at` is None for legacy banners that pre-date the
    timestamp field. Float seconds since the unix epoch.
    """
    m = _FINISH_BANNER_RE.search(line)
    if not m:
        return None
    g = m.groupdict()
    finished_at_str = g.get("finished_at")
    return {
        "kind": g["kind"],
        "agent": g.get("agent") or DEFAULT_AGENT,
        "skill": g["skill"],
        "pid": int(g["pid"]),
        "verdict": g["verdict"],
        "finished_at": float(finished_at_str) if finished_at_str else None,
    }
