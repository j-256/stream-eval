"""Tests for stream_eval.pool: the dispatcher loop, target-workers
adjustment, drain-on-decrement, pause/resume."""
import threading
import time
from unittest import mock

import pytest

from stream_eval.pool import (
    Dispatcher,
    DispatcherState,
    WorkerSlot,
)


class FakeWorker:
    """In-process stand-in for a `claude -p` subprocess. Sleeps for
    `duration` seconds, then 'completes.' Tests use this instead of
    real subprocesses so they're fast and deterministic."""
    def __init__(self, task, duration=0.05):
        self.task = task
        self.duration = duration
        self._thread = None
        self._done = threading.Event()
        self.result = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        time.sleep(self.duration)
        self.result = {"task": self.task, "status": "ok"}
        self._done.set()

    def is_done(self):
        return self._done.is_set()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout=timeout)


def test_worker_slot_holds_task_and_started_at():
    fake = FakeWorker(task="t1")
    slot = WorkerSlot(task="t1", worker=fake)
    assert slot.task == "t1"
    assert slot.worker is fake
    assert slot.started_at > 0


def test_dispatcher_initial_target_workers():
    d = Dispatcher(target_workers=4, spawn_worker=lambda t: FakeWorker(t))
    assert d.target_workers == 4
    assert d.state == DispatcherState.RUNNING


def test_dispatcher_set_target_workers_thread_safe():
    d = Dispatcher(target_workers=4, spawn_worker=lambda t: FakeWorker(t))
    threads = []
    for n in (1, 8, 2, 10, 3):
        t = threading.Thread(target=lambda v=n: setattr(d, "target_workers", v))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Final value: one of 1, 8, 2, 10, 3. We don't care which (race);
    # we DO care that it's a valid int that was set.
    assert d.target_workers in (1, 8, 2, 10, 3)


def test_dispatcher_target_workers_minimum_zero_floor():
    d = Dispatcher(target_workers=4, spawn_worker=lambda t: FakeWorker(t))
    d.target_workers = 0
    # Floor at 0 is allowed (== pause). Floor at -1 is clamped to 0.
    assert d.target_workers == 0
    d.target_workers = -5
    assert d.target_workers == 0


def test_dispatcher_submit_and_run_completes_all_tasks():
    d = Dispatcher(target_workers=2, spawn_worker=lambda t: FakeWorker(t, duration=0.02))
    for i in range(5):
        d.submit(f"t{i}")
    d.run_until_complete(timeout=5)
    completed = list(d.drain_completed())
    assert sorted(c["task"] for c in completed) == ["t0", "t1", "t2", "t3", "t4"]


def test_dispatcher_respects_target_workers_ceiling():
    """At any instant, len(active) <= target_workers."""
    d = Dispatcher(
        target_workers=2,
        spawn_worker=lambda t: FakeWorker(t, duration=0.05),
    )
    for i in range(10):
        d.submit(f"t{i}")

    observed_max = [0]

    def observe():
        while d.state != DispatcherState.STOPPED:
            with d._lock:
                observed_max[0] = max(observed_max[0], len(d._active))
            time.sleep(0.005)

    t = threading.Thread(target=observe, daemon=True)
    t.start()
    d.run_until_complete(timeout=5)
    t.join(timeout=1)
    assert observed_max[0] <= 2


def _active_count(d):
    """Read len(d._active) under the dispatcher's lock. Reading the
    list outside the lock is a race because run_until_complete reassigns
    _active to a new list each iteration."""
    with d._lock:
        return len(d._active)


def test_dispatcher_drain_on_decrement_no_new_spawns_until_under_target():
    """When target decreases mid-run, no new spawns until active < new target."""
    d = Dispatcher(
        target_workers=4,
        spawn_worker=lambda t: FakeWorker(t, duration=0.1),
    )
    for i in range(8):
        d.submit(f"t{i}")

    # Start running in a thread.
    runner = threading.Thread(
        target=lambda: d.run_until_complete(timeout=5), daemon=True
    )
    runner.start()

    # Wait for active to ramp up to 4.
    for _ in range(50):
        if _active_count(d) >= 4:
            break
        time.sleep(0.005)
    assert _active_count(d) == 4

    # Decrement target. New workers should not spawn until active < 2.
    d.target_workers = 2

    # Active will shrink as workers finish naturally.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if _active_count(d) <= 2:
            break
        time.sleep(0.01)
    assert _active_count(d) <= 2

    runner.join(timeout=5)


def test_dispatcher_pause_blocks_new_spawns_resume_unblocks():
    d = Dispatcher(
        target_workers=2,
        spawn_worker=lambda t: FakeWorker(t, duration=0.02),
    )
    for i in range(5):
        d.submit(f"t{i}")

    d.pause()
    assert d.state == DispatcherState.PAUSED

    runner = threading.Thread(
        target=lambda: d.run_until_complete(timeout=5), daemon=True
    )
    runner.start()
    time.sleep(0.1)

    # No spawns should have happened during pause.
    assert _active_count(d) == 0

    d.resume()
    assert d.state == DispatcherState.RUNNING
    runner.join(timeout=3)
    completed = list(d.drain_completed())
    assert len(completed) == 5


def test_dispatcher_stop_breaks_run_loop_promptly():
    """stop() must transition the dispatcher to STOPPED so that
    run_until_complete exits on its next poll cycle. This is the
    abort-on-first-timeout path: we want the driver thread to return,
    not spin forever waiting for in-flight workers to drain."""
    d = Dispatcher(
        target_workers=2,
        spawn_worker=lambda t: FakeWorker(t, duration=0.5),
    )
    for i in range(10):
        d.submit(f"t{i}")

    runner = threading.Thread(
        target=lambda: d.run_until_complete(timeout=5), daemon=True
    )
    runner.start()

    # Wait for at least one worker to be active.
    for _ in range(50):
        if _active_count(d) >= 1:
            break
        time.sleep(0.005)

    # Stop the dispatcher.
    d.stop()

    # The runner thread must exit promptly; allow ~5x the poll interval
    # plus a small fudge factor.
    runner.join(timeout=1.0)
    assert not runner.is_alive(), \
        "run_until_complete did not exit after stop()"
    assert d.state == DispatcherState.STOPPED
