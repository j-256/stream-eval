"""Scenario builders + FakeState handle.

Each scenario synthesizes an .output file (via runner.format_*_banner
and runner._format_progress so the format stays in sync with the
real runner) and optionally starts a FakeSocketServer for an active
harness. The output files live under a base_dir that mirrors the
real ~/.claude/projects/<project-slug>/ layout so find_output_files
walks them naturally.

Adding a scenario:
1. Write a `_scenario_<name>(builder)` function that calls
   builder.start_eval(...) and builder.complete_run(...) /
   builder.finish_eval(...) as needed.
2. Register it in SCENARIOS at the bottom.
3. Add a test in tests/test_fake.py.

The dashboard reads .output files from ~/.claude/projects via
ps._output_paths -> find_output_files. To make a fake scenario
visible to a real dashboard, call make_fake_state() and then point
the dashboard at the temp dir, e.g. by patching ps._output_paths
in tests, or by symlinking the temp dir under ~/.claude/projects
for interactive smoke testing (the __main__ entry handles this).
"""
import os
import tempfile
from pathlib import Path

from stream_eval.fake.socket_server import FakeSocketServer
from stream_eval.runner import (
    _format_progress,
    format_finish_banner,
    format_startup_banner,
)


# Fake harness pids start well above typical real pids on macOS/Linux
# so they're unlikely to collide with anything actually running. The
# fake never spawns real processes, so these pids are ledger entries
# only -- the dashboard's os.kill liveness probe will return False
# unless we explicitly mark them alive (see is_pid_alive helper below).
_FAKE_PID_BASE = 90000


class FakeState:
    """Handle returned from make_fake_state. Holds the temp dir,
    the running listener threads, and tear-down logic.

    Use as a context manager (recommended) or call close() manually
    when done."""

    def __init__(self, base_dir, output_paths, sockets, fake_pids):
        self.base_dir = base_dir
        self.output_paths = output_paths
        self.sockets = sockets  # list[FakeSocketServer]
        self.fake_pids = fake_pids  # set[int]
        self._closed = False

    def is_pid_alive(self, pid):
        """Predicate for state.build_state's is_pid_alive parameter:
        any pid we synthesized is 'alive' as long as this FakeState
        hasn't been closed. Other pids are not.

        This lets tests pass the same FakeState through both layers --
        the .output files are bound to fake harness pids, and the
        liveness probe agrees they're alive."""
        if self._closed:
            return False
        return pid in self.fake_pids

    def close(self):
        if self._closed:
            return
        self._closed = True
        for sock in self.sockets:
            try:
                sock.close()
            except Exception:
                pass
        # Best-effort clean up: remove the temp dir if we created it.
        if self.base_dir and os.path.isdir(self.base_dir):
            for p in Path(self.base_dir).rglob("*"):
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass
            try:
                # Remove empty subdirs bottom-up.
                for p in sorted(
                    Path(self.base_dir).rglob("*"),
                    key=lambda p: -len(p.parts),
                ):
                    if p.is_dir():
                        try:
                            p.rmdir()
                        except OSError:
                            pass
                Path(self.base_dir).rmdir()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Builder:
    """Per-scenario builder. Tracks fake-pid allocation + socket
    cleanup. Each `start_eval` returns a handle the caller appends
    progress/finish lines to."""

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._next_pid = _FAKE_PID_BASE
        self.sockets = []
        self.fake_pids = set()
        self.output_paths = []

    def alloc_pid(self):
        pid = self._next_pid
        self._next_pid += 1
        self.fake_pids.add(pid)
        return pid

    def start_eval(self, *, skill, kind, total_fixtures, runs,
                   workers=4, eval_path=None, pid=None,
                   live_socket=False, file_name=None):
        """Open an .output file with a startup banner. Returns an
        _EvalHandle the caller fills with progress lines and an
        optional finish banner.

        If live_socket=True, also spin up a FakeSocketServer at
        /tmp/stream-eval-<pid>.sock so the dashboard's per-row
        controls have something to talk to. Workers/state are
        seeded from the `workers` arg.
        """
        if pid is None:
            pid = self.alloc_pid()
        else:
            self.fake_pids.add(pid)
        eval_path = eval_path or f"evals/{skill}/{kind}-eval.json"
        file_name = file_name or f"{skill}-{kind}-{pid}.output"
        out_path = self.base_dir / file_name
        with out_path.open("w") as f:
            f.write(format_startup_banner(
                kind=kind, skill=skill, eval_path=eval_path,
                runs=runs, workers=workers,
                total_fixtures=total_fixtures, pid=pid,
            ) + "\n")
        self.output_paths.append(out_path)
        if live_socket:
            sock = FakeSocketServer(
                f"/tmp/stream-eval-{pid}.sock",
                initial_workers=workers,
            )
            self.sockets.append(sock)
        return _EvalHandle(
            out_path=out_path, pid=pid, skill=skill, kind=kind,
            total_fixtures=total_fixtures, runs=runs,
        )


class _EvalHandle:
    """Filled in by scenario builders -- one progress line per
    completed run, optionally a finish banner."""

    def __init__(self, *, out_path, pid, skill, kind,
                 total_fixtures, runs):
        self.out_path = out_path
        self.pid = pid
        self.skill = skill
        self.kind = kind
        self.total_fixtures = total_fixtures
        self.runs = runs
        self.completed = 0
        self.total_runs = total_fixtures * runs

    def complete_run(self, *, fixture_id, run, pass_,
                     elapsed=10.0, retries=0,
                     timeout_reason="none", first_tool="Skill",
                     first_skill=None, failed_asserts=0,
                     contaminated=False, query="fake query"):
        """Append one progress line."""
        first_skill = first_skill or self.skill
        self.completed += 1
        line = _format_progress(
            n=self.completed, total=self.total_runs, kind=self.kind,
            pass_=pass_, fixture_id=fixture_id, run_idx=run,
            elapsed_seconds=elapsed, total_retries=retries,
            timeout_reason=timeout_reason,
            first_tool=first_tool, first_skill=first_skill,
            failed_asserts=failed_asserts, contaminated=contaminated,
            query=query,
        )
        with self.out_path.open("a") as f:
            f.write(line + "\n")

    def finish_eval(self, verdict):
        """Append the finish banner (verdict='completed' or 'aborted')."""
        with self.out_path.open("a") as f:
            f.write(format_finish_banner(
                kind=self.kind, skill=self.skill,
                verdict=verdict, pid=self.pid,
            ) + "\n")


# ---------- scenarios ----------

def _scenario_active_clean(b):
    h = b.start_eval(
        skill="dsc-scrape", kind="trigger",
        total_fixtures=10, runs=1, live_socket=True,
    )
    for i in range(3):
        h.complete_run(fixture_id=f"q{i}", run=1, pass_=True,
                       query=f"clean run {i}")


def _scenario_active_with_failures(b):
    h = b.start_eval(
        skill="dsc-endpoint-help", kind="trigger",
        total_fixtures=10, runs=1, live_socket=True,
    )
    h.complete_run(fixture_id="q0", run=1, pass_=True)
    h.complete_run(fixture_id="q1", run=1, pass_=False, retries=2,
                   first_tool="Bash", first_skill="-", query="missed it")
    h.complete_run(fixture_id="q2", run=1, pass_=True)
    h.complete_run(fixture_id="q3", run=1, pass_=False, retries=1,
                   query="missed again")


def _scenario_active_with_contamination(b):
    h = b.start_eval(
        skill="dsc-scenario", kind="synthesis",
        total_fixtures=5, runs=2, live_socket=True,
    )
    h.complete_run(fixture_id="q0", run=1, pass_=True, contaminated=True,
                   query="contaminated but technically passed")
    h.complete_run(fixture_id="q0", run=2, pass_=True)
    h.complete_run(fixture_id="q1", run=1, pass_=False, contaminated=True,
                   query="contaminated and failed")


def _scenario_concurrent(b):
    """Two active evals of different skills, side by side. Per-row
    controls must address them independently -- this is the scenario
    that surfaced the routing bug originally."""
    h1 = b.start_eval(
        skill="dsc-scrape", kind="trigger",
        total_fixtures=8, runs=1, live_socket=True,
    )
    for i in range(4):
        h1.complete_run(fixture_id=f"q{i}", run=1, pass_=True)
    h2 = b.start_eval(
        skill="dsc-scenario", kind="synthesis",
        total_fixtures=4, runs=2, live_socket=True,
    )
    for i in range(2):
        h2.complete_run(fixture_id=f"f{i}", run=1, pass_=True)


def _scenario_completed(b):
    h = b.start_eval(
        skill="dsc-scrape", kind="trigger",
        total_fixtures=3, runs=1, live_socket=False,
    )
    for i in range(3):
        h.complete_run(fixture_id=f"q{i}", run=1, pass_=True)
    h.finish_eval("completed")


def _scenario_aborted(b):
    """Finish banner verdict=aborted -- the harness bailed early
    (timeout or throttle). Status state machine should pick this up
    directly from the banner."""
    h = b.start_eval(
        skill="dsc-endpoint-help", kind="synthesis",
        total_fixtures=10, runs=1, live_socket=False,
    )
    for i in range(3):
        h.complete_run(fixture_id=f"q{i}", run=1, pass_=True)
    h.finish_eval("aborted")


def _scenario_aborted_no_finish_banner(b):
    """Startup banner present, no progress for a while, no finish
    banner -- the runner was killed (Ctrl-C, OOM) before it could
    stamp the verdict. Status falls through to pid-liveness; if the
    pid isn't alive (FakeState's default), status='aborted'."""
    h = b.start_eval(
        skill="dsc-triage-fake", kind="trigger",
        total_fixtures=10, runs=1, live_socket=False,
    )
    h.complete_run(fixture_id="q0", run=1, pass_=True)
    # No finish banner.


def _scenario_legacy(b):
    """An .output file written before F.5 -- no pid in the banner.
    Should still parse; the row renders with status='unknown' and
    no controls."""
    out_path = b.base_dir / "legacy.output"
    out_path.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=3 workers=2 "
        "total_fixtures=2 ===\n"
        "[1/6] kind=trigger pass=True fixture_id=q0 run=1 elapsed=8s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": legacy without pid\n"
    )
    b.output_paths.append(out_path)


def _scenario_over_cap(b):
    """Seven completed evals of the same skill with progressively
    older mtimes. With per_skill_cap=5 the dashboard hides the two
    oldest; the remaining 5 render in mtime-desc order."""
    import time
    now = time.time()
    for i in range(7):
        h = b.start_eval(
            skill="dsc-cap-test", kind="trigger",
            total_fixtures=2, runs=1, live_socket=False,
        )
        h.complete_run(fixture_id="q0", run=1, pass_=True)
        h.complete_run(fixture_id="q1", run=1, pass_=True)
        h.finish_eval("completed")
        # Stamp progressively older mtimes so the cap predictably
        # drops the oldest two.
        os.utime(h.out_path, (now - i * 60, now - i * 60))


def _scenario_full_spread(b):
    """One of each above. The visual smoke benchmark: every state
    the dashboard handles should be reachable from one screen."""
    _scenario_active_clean(b)
    _scenario_active_with_failures(b)
    _scenario_active_with_contamination(b)
    _scenario_completed(b)
    _scenario_aborted(b)
    _scenario_aborted_no_finish_banner(b)
    _scenario_legacy(b)


SCENARIOS = {
    "active-clean": _scenario_active_clean,
    "active-with-failures": _scenario_active_with_failures,
    "active-with-contamination": _scenario_active_with_contamination,
    "concurrent": _scenario_concurrent,
    "completed": _scenario_completed,
    "aborted": _scenario_aborted,
    "aborted-no-finish-banner": _scenario_aborted_no_finish_banner,
    "legacy": _scenario_legacy,
    "over-cap": _scenario_over_cap,
    "full-spread": _scenario_full_spread,
}


def make_fake_state(scenarios, *, base_dir=None):
    """Build one or more named scenarios into a single FakeState.

    `scenarios` can be a single name (`"concurrent"`) or an iterable
    (`["concurrent", "over-cap", "legacy"]`). Multiple scenarios
    share a base_dir, a pid allocator, and a sockets list, so the
    rendered dashboard shows them all at once -- the realistic case,
    where active runs, completed runs, aborted runs, and legacy
    .output files coexist.

    Returns a FakeState the caller closes when done. If base_dir is
    None a tempdir is allocated and removed on close.
    """
    if isinstance(scenarios, str):
        scenarios = [scenarios]
    names = list(scenarios)
    for name in names:
        if name not in SCENARIOS:
            raise KeyError(
                f"unknown scenario {name!r}; "
                f"choices: {sorted(SCENARIOS)}"
            )
    if base_dir is None:
        base_dir = tempfile.mkdtemp(prefix="stream-eval-fake-")
    b = _Builder(base_dir)
    for name in names:
        SCENARIOS[name](b)
    return FakeState(
        base_dir=base_dir,
        output_paths=list(b.output_paths),
        sockets=list(b.sockets),
        fake_pids=set(b.fake_pids),
    )
