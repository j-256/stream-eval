"""Build DashboardState from .output files.

The runner emits a startup banner plus one progress line per completed
run. This module re-reads those files and aggregates per-(skill, kind)
into a structure the templates render.

Public surface:
- DashboardState: one snapshot of all skill rows.
- DashboardRow: per-(skill, kind) row.
- DashboardCell: per-(fixture_id, run) cell in the segmented bar.
- build_state(output_paths): construct DashboardState from .output paths.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import os

from stream_eval.monitor.parsing import (
    parse_finish_banner,
    parse_progress_line,
    parse_startup_banner,
)


@dataclass
class DashboardCell:
    fixture_id: str
    run: int
    pass_: Optional[bool]  # None = pending
    contaminated: bool = False


@dataclass
class DashboardRow:
    skill: str
    kind: str
    total_fixtures: int
    runs: int
    cells: list = field(default_factory=list)
    # Harness pid stamped by the runner's startup banner. None for legacy
    # banners (pre-F.5) -- those rows render with `status="unknown"` and
    # no per-row controls. Used to bind worker-control buttons to the
    # right /tmp/stream-eval-<pid>.sock and to discover this row's
    # claude pids in psutil for the inline Active Workers table.
    harness_pid: Optional[int] = None
    # Status state machine: "active" / "completed" / "aborted" /
    # "unknown".
    #   - "completed": runner emitted the finish banner with verdict=completed
    #   - "aborted": finish banner with verdict=aborted, OR no finish
    #     banner but harness pid is dead (the runner crashed before it
    #     could stamp the verdict, or the user Ctrl-Cd it)
    #   - "active": no finish banner and harness pid is still alive
    #   - "unknown": legacy .output file with no pid in the banner; we
    #     can't tell if the harness is alive
    status: str = "unknown"
    # Mtime of the source .output file -- used to break ties when more
    # than the per-skill cap of completed rows survives the window.
    mtime: float = 0.0
    # Ctime of the source .output file. Approximates the harness's
    # start time -- the runner creates the file immediately after the
    # tee installs. Used by the stop-confirmation modal to render
    # "running for X minutes" without parsing the banner timestamp.
    ctime: float = 0.0


@dataclass
class DashboardState:
    rows: list = field(default_factory=list)


def build_state(output_paths, *, is_pid_alive=None, per_skill_cap=None):
    """Parse each .output file and build per-(skill, kind, harness_pid) rows.

    Rows are keyed by (skill, kind, harness_pid) so two concurrent evals
    of the same skill+kind get distinct rows -- otherwise their cells
    would interleave and worker-control buttons would route to the wrong
    socket. Legacy .output files written before F.5 stamped pid in the
    banner end up keyed with harness_pid=None, which is fine: there's
    only ever one such row per (skill, kind) and it renders with no
    controls (status='unknown').

    is_pid_alive is an optional callable (pid -> bool) used to decide
    whether a row without a finish banner is "active" (pid still
    running) or "aborted" (pid gone, runner crashed before it could
    stamp the verdict). Defaults to an os.kill(sig=0) probe; tests
    inject a fake to avoid system calls.

    per_skill_cap, if set, limits each (skill, kind) to at most that
    many rows: active rows always win, then completed/aborted/unknown
    sorted by mtime descending. Defaults to STREAM_EVAL_PER_SKILL_CAP
    or 5. Set to 0 to disable the cap entirely.

    Each .output's mtime is recorded on its rows so the caller can rank
    rows by recency when applying the cap.
    """
    if per_skill_cap is None:
        per_skill_cap = int(os.environ.get("STREAM_EVAL_PER_SKILL_CAP", "5"))
    if is_pid_alive is None:
        is_pid_alive = _default_is_pid_alive
    rows_by_key = {}
    finish_verdicts = {}
    for path in output_paths:
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        try:
            stat = Path(path).stat()
            mtime = stat.st_mtime
            ctime = stat.st_ctime
        except OSError:
            mtime = 0.0
            ctime = 0.0
        current_key = None
        for line in text.splitlines():
            banner = parse_startup_banner(line)
            if banner:
                key = (banner["skill"], banner["kind"], banner["pid"])
                row = rows_by_key.setdefault(
                    key,
                    DashboardRow(
                        skill=banner["skill"],
                        kind=banner["kind"],
                        total_fixtures=banner["total_fixtures"],
                        runs=banner["runs"],
                        harness_pid=banner["pid"],
                        mtime=mtime,
                        ctime=ctime,
                    ),
                )
                row.total_fixtures = banner["total_fixtures"]
                row.runs = banner["runs"]
                row.mtime = max(row.mtime, mtime)
                # ctime: keep the earliest -- the file's original
                # creation time, even if multiple banners (rare).
                if row.ctime == 0.0 or (ctime > 0 and ctime < row.ctime):
                    row.ctime = ctime
                current_key = key
                continue

            finish = parse_finish_banner(line)
            if finish:
                # The finish banner doesn't carry the eval path, so we
                # can't reconstruct the full key here. Index by
                # (skill, kind, pid); for legacy/no-pid rows we won't
                # see a finish banner anyway.
                finish_verdicts[(finish["skill"], finish["kind"], finish["pid"])] = (
                    finish["verdict"]
                )
                continue

            prog = parse_progress_line(line)
            if not prog:
                continue
            if current_key is None:
                continue
            row = rows_by_key.setdefault(
                current_key,
                DashboardRow(
                    skill=current_key[0], kind=current_key[1],
                    total_fixtures=0, runs=0,
                    harness_pid=current_key[2],
                    mtime=mtime,
                    ctime=ctime,
                ),
            )
            row.cells.append(DashboardCell(
                fixture_id=prog["fixture_id"],
                run=prog["run"],
                pass_=prog["pass_"],
                contaminated=prog["contaminated"],
            ))

    for key, row in rows_by_key.items():
        verdict = finish_verdicts.get(key)
        if verdict in ("completed", "aborted"):
            row.status = verdict
        elif row.harness_pid is None:
            row.status = "unknown"
        elif is_pid_alive(row.harness_pid):
            row.status = "active"
        else:
            row.status = "aborted"

    rows = list(rows_by_key.values())
    if per_skill_cap and per_skill_cap > 0:
        rows = _apply_per_skill_cap(rows, per_skill_cap)
    # Order rows by status bucket first (active runs are usually what
    # the operator is looking at), then by recency within each bucket.
    # An eval that just finished lands at the top of the completed
    # bucket -- right below your active rows -- rather than getting
    # alphabetically buried mid-list.
    rows.sort(key=lambda r: (_status_order(r.status), -r.mtime,
                              r.skill, r.kind))
    return DashboardState(rows=rows)


_STATUS_ORDER = {"active": 0, "aborted": 1, "completed": 2, "unknown": 3}


def _status_order(status):
    return _STATUS_ORDER.get(status, 99)


def _apply_per_skill_cap(rows, cap):
    """Keep at most `cap` rows per (skill, kind). Active rows always
    win (we never hide an in-progress eval, even if it's older). Among
    non-active rows, the youngest by mtime survive."""
    by_group = {}
    for r in rows:
        by_group.setdefault((r.skill, r.kind), []).append(r)
    kept = []
    for group_rows in by_group.values():
        active = [r for r in group_rows if r.status == "active"]
        non_active = [r for r in group_rows if r.status != "active"]
        non_active.sort(key=lambda r: -r.mtime)
        slots = max(cap - len(active), 0)
        kept.extend(active)
        kept.extend(non_active[:slots])
    return kept


def _default_is_pid_alive(pid):
    """Return True if `pid` is currently a running process. Uses os.kill
    with signal 0 (the no-op probe) -- raises ProcessLookupError if the
    pid doesn't exist, PermissionError if it exists but we can't signal
    it (counts as alive for our purposes; another user's process is
    still a live process)."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
