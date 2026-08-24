"""Tests for stream_eval.subprocess.

Two surfaces:
  - RetryClock: pure state machine with injectable now_fn. Unit-tested
    here without spawning subprocesses or sleeping.
  - run_with_retry_aware_bail: integration tests that exercise the full
    file/process glue. These spawn small `python3 -c` scripts that emit
    timed events, so they take a few real seconds to run.

Most behavioral coverage lives on RetryClock; the integration tests
just confirm the wiring.
"""
import json
import os
import subprocess as _subprocess
import sys
import textwrap
import time

import pytest

from stream_eval.subprocess import (
    RetryClock,
    classify_line,
    run_with_retry_aware_bail,
)


def _retry_event(attempt, max_retries=10):
    return {
        "type": "system",
        "subtype": "api_retry",
        "attempt": attempt,
        "max_retries": max_retries,
    }


def _output_event():
    """An event RetryClock should treat as output-bearing (closes a
    retry window). The CLI emits these for assistant text, tool_use,
    result, etc.; we use a tool_use shape here as a representative."""
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}


# -----------------------------------------------------------------------
# classify_line: bucket extension
# -----------------------------------------------------------------------

def test_classify_line_retry_event_returns_retry_bucket():
    kind, info = classify_line(_retry_event(2, 10))
    assert kind == "retry"
    assert info == {"attempt": 2, "max_retries": 10}


def test_classify_line_assistant_event_returns_output_bucket():
    """Assistant events bear output (model produced something) and
    therefore close any in-flight retry window."""
    kind, info = classify_line(_output_event())
    assert kind == "output"
    assert info is None


def test_classify_line_tool_use_event_returns_output_bucket():
    kind, info = classify_line({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}})
    assert kind == "output"


def test_classify_line_result_event_returns_output_bucket():
    """The terminal result event also counts as output; the run is over
    by the time we see it, but treating it as output keeps the state
    machine consistent (no orphaned retry window)."""
    kind, info = classify_line({"type": "result", "subtype": "success"})
    assert kind == "output"


def test_classify_line_unrelated_system_event_returns_none():
    """System events that aren't api_retry and don't bear output: the
    state machine should ignore them. Examples: hook_response,
    init events, anything else the CLI might add later."""
    kind, info = classify_line({"type": "system", "subtype": "init"})
    assert kind is None
    assert info is None


def test_classify_line_non_dict_returns_none():
    kind, info = classify_line("not a dict")
    assert kind is None
    assert info is None


# -----------------------------------------------------------------------
# RetryClock state machine
# -----------------------------------------------------------------------

class _FakeClock:
    """Manually-advanced clock for RetryClock tests."""
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_retry_clock_starts_idle_with_zero_time_in_retries():
    fc = _FakeClock()
    rc = RetryClock(now_fn=fc)
    assert rc.time_in_retries == 0
    assert not rc.in_retry


def test_retry_clock_retry_event_opens_window():
    fc = _FakeClock()
    rc = RetryClock(now_fn=fc)
    rc.on_event("retry", {"attempt": 1, "max_retries": 10})
    assert rc.in_retry
    # No closed time yet -- the window is still open.
    assert rc.time_in_retries == 0


def test_retry_clock_output_event_closes_window():
    fc = _FakeClock()
    rc = RetryClock(now_fn=fc)
    rc.on_event("retry", {"attempt": 1, "max_retries": 10})
    fc.advance(60)  # 60s of backoff wait
    rc.on_event("output", None)
    assert not rc.in_retry
    assert rc.time_in_retries == 60


def test_retry_clock_subsequent_retry_event_keeps_window_open():
    """Multiple api_retry events without intervening output should hold
    the window open continuously, not reset it."""
    fc = _FakeClock()
    rc = RetryClock(now_fn=fc)
    rc.on_event("retry", {"attempt": 1, "max_retries": 10})
    fc.advance(60)
    rc.on_event("retry", {"attempt": 2, "max_retries": 10})
    fc.advance(60)
    rc.on_event("retry", {"attempt": 3, "max_retries": 10})
    # Window still open; no time accounted yet.
    assert rc.in_retry
    assert rc.time_in_retries == 0
    # An output event closes the window. Window opened at t=0 (event 1)
    # and closes at t=120 (after the two 60s advances), so 120s total.
    rc.on_event("output", None)
    assert rc.time_in_retries == 120


def test_retry_clock_burst_then_resume_then_burst_accumulates():
    """Two distinct retry windows separated by output should sum."""
    fc = _FakeClock()
    rc = RetryClock(now_fn=fc)

    rc.on_event("retry", {"attempt": 1, "max_retries": 10})
    fc.advance(60)
    rc.on_event("output", None)
    assert rc.time_in_retries == 60

    fc.advance(30)  # model thinking time
    rc.on_event("retry", {"attempt": 1, "max_retries": 10})
    fc.advance(45)
    rc.on_event("output", None)
    assert rc.time_in_retries == 105


def test_retry_clock_close_open_window_idempotent():
    """If we close an already-closed window (e.g. process exit after
    output already arrived), it's a no-op."""
    fc = _FakeClock()
    rc = RetryClock(now_fn=fc)
    rc.on_event("retry", {"attempt": 1, "max_retries": 10})
    fc.advance(60)
    rc.on_event("output", None)
    fc.advance(30)
    rc.close_open_window()  # nothing in-flight
    assert rc.time_in_retries == 60


def test_retry_clock_close_open_window_during_retry_finalizes():
    """Used by the wrapper when the process exits or is killed mid-retry
    so the accounting still adds up for diagnostics."""
    fc = _FakeClock()
    rc = RetryClock(now_fn=fc)
    rc.on_event("retry", {"attempt": 1, "max_retries": 10})
    fc.advance(45)
    rc.close_open_window()
    assert not rc.in_retry
    assert rc.time_in_retries == 45


def test_retry_clock_effective_elapsed_excludes_retry_time():
    fc = _FakeClock(start=1000.0)
    rc = RetryClock(now_fn=fc, t0=1000.0)

    fc.advance(5)              # 5s of model thinking
    rc.on_event("retry", {"attempt": 1, "max_retries": 10})
    fc.advance(60)             # 60s of retry backoff
    rc.on_event("output", None)
    fc.advance(10)             # 10s more thinking

    # Total wall time = 75s; retry-adjusted = 15s.
    assert rc.effective_elapsed() == pytest.approx(15.0)


def test_retry_clock_effective_elapsed_during_open_retry():
    """While inside a retry window, effective_elapsed should subtract
    the in-flight portion too -- otherwise the wall-clock check could
    fire while we're still legitimately waiting on backoff."""
    fc = _FakeClock(start=1000.0)
    rc = RetryClock(now_fn=fc, t0=1000.0)

    fc.advance(20)
    rc.on_event("retry", {"attempt": 1, "max_retries": 10})
    fc.advance(100)  # still inside the retry window
    # 120s total wall, but 100s of it is current retry backoff -> 20s effective.
    assert rc.effective_elapsed() == pytest.approx(20.0)


def test_retry_clock_absolute_elapsed_includes_retry_time():
    """Absolute elapsed (used for the 4*timeout backstop) does NOT
    subtract retry time."""
    fc = _FakeClock(start=1000.0)
    rc = RetryClock(now_fn=fc, t0=1000.0)

    fc.advance(5)
    rc.on_event("retry", {"attempt": 1, "max_retries": 10})
    fc.advance(60)
    rc.on_event("output", None)
    fc.advance(10)

    assert rc.absolute_elapsed() == pytest.approx(75.0)


# -----------------------------------------------------------------------
# Integration: run_with_retry_aware_bail
# -----------------------------------------------------------------------

def _emitter_script(events_with_delays):
    """Build a python -c script that emits a sequence of (delay, event)
    pairs to stdout as JSONL, then exits 0. The events are dicts; delays
    are seconds before each emission.
    """
    lines = ["import json, sys, time"]
    for delay, ev in events_with_delays:
        lines.append(f"time.sleep({delay})")
        lines.append(f"print(json.dumps({ev!r}))")
        lines.append("sys.stdout.flush()")
    return "; ".join(lines)


def test_integration_retries_dont_count_against_wall_clock(tmp_path):
    """A run that spends most of its wall time emitting api_retry events
    should NOT trip the wall-clock backstop, because retry-backoff time
    is excluded from the deadline."""
    transcript = tmp_path / "out.jsonl"
    # Total ~3.5s wall. ~3s of retry "backoff" + 0.5s of effective work.
    # Wall-clock timeout=2s. Old behavior: fail. New behavior: pass.
    events = [
        (0.1, _retry_event(1, 10)),
        (1.0, _retry_event(2, 10)),  # 1s "in retry"
        (1.0, _retry_event(3, 10)),
        (1.0, _output_event()),       # closes the 3s window
        (0.4, {"type": "result", "subtype": "success"}),
    ]
    cmd = [sys.executable, "-c", _emitter_script(events)]

    bail = run_with_retry_aware_bail(
        cmd=cmd,
        stdout_path=str(transcript),
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        timeout=2,
    )
    assert not bail["wall_timed_out"], (
        f"wall clock fired despite retry-backoff exclusion. "
        f"time_in_retries={bail.get('time_in_retries')}"
    )
    assert not bail["wall_timed_out_in_retry"]
    assert bail["exit_code"] == 0
    assert bail["time_in_retries"] >= 2.5  # at least the three 1s gaps


def test_integration_model_thinking_alone_still_triggers_wall_clock(tmp_path):
    """No retries; just a slow process. The wall clock should still fire."""
    transcript = tmp_path / "out.jsonl"
    # Sleep 5s without emitting any retry events. Timeout=1s.
    events = [
        (5.0, _output_event()),
    ]
    cmd = [sys.executable, "-c", _emitter_script(events)]

    bail = run_with_retry_aware_bail(
        cmd=cmd,
        stdout_path=str(transcript),
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        timeout=1,
    )
    assert bail["wall_timed_out"]
    assert not bail["wall_timed_out_in_retry"]


def test_integration_stuck_during_retry_eventually_bails(tmp_path):
    """A process that emits one retry event and then nothing forever
    must still bail via the absolute backstop (4 * timeout).

    Uses timeout=1 (-> 4s backstop) so the blind period before the
    first retry event registers (<= one poll cycle = 0.5s) doesn't
    accidentally trip the retry-aware clock.
    """
    transcript = tmp_path / "out.jsonl"
    # One retry event, then sleep well past 4*timeout=4s.
    events = [
        (0.1, _retry_event(1, 10)),
        (15.0, _output_event()),
    ]
    cmd = [sys.executable, "-c", _emitter_script(events)]

    bail = run_with_retry_aware_bail(
        cmd=cmd,
        stdout_path=str(transcript),
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        timeout=1,
    )
    assert bail["wall_timed_out_in_retry"]
    # The regular wall_clock signal should NOT also be set; this is
    # specifically the in-retry stuck case.
    assert not bail["wall_timed_out"]


def test_integration_retry_budget_exhausted_still_takes_priority(tmp_path):
    """Existing behavior: api_retry-exhaustion fires first, regardless of
    wall-clock state. This test confirms the retry-aware change didn't
    break that priority."""
    transcript = tmp_path / "out.jsonl"
    events = [
        (0.1, _retry_event(10, 10)),  # exhausted on first event
        (5.0, _output_event()),       # never reached
    ]
    cmd = [sys.executable, "-c", _emitter_script(events)]

    bail = run_with_retry_aware_bail(
        cmd=cmd,
        stdout_path=str(transcript),
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        timeout=10,
    )
    assert bail["retry_budget_exhausted"]
    assert not bail["wall_timed_out"]
    assert not bail["wall_timed_out_in_retry"]
