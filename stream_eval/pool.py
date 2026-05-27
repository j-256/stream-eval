"""Dynamic worker pool for stream-eval.

Replaces concurrent.futures.ProcessPoolExecutor's fixed max_workers
with a hand-rolled dispatcher whose target-workers count can change
mid-run. Supports drain-on-decrement (in-flight tasks finish; no new
ones spawn until active < target), pause/resume, and worker counts
controlled from any thread (signal handler, socket listener,
in-process).

Public surface:
- Dispatcher: orchestrates pending queue, active workers, completed queue.
- WorkerSlot: tracks one running worker.
- DispatcherState: enum of run states (RUNNING, PAUSED, STOPPED).
"""
import enum
import threading
import time
from collections import deque


class DispatcherState(enum.Enum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class WorkerSlot:
    """One running worker. The dispatcher polls is_done()/result on each
    loop iteration; when is_done() returns True, the slot is reaped and
    its result moves to the completed queue."""
    __slots__ = ("task", "worker", "started_at")

    def __init__(self, task, worker):
        self.task = task
        self.worker = worker
        self.started_at = time.time()


class Dispatcher:
    """Worker-pool dispatcher with a live-adjustable target_workers count.

    The caller provides:
    - target_workers: initial ceiling on concurrent workers.
    - spawn_worker: callable taking a task and returning a Worker. The
      Worker must have .start(), .is_done(), .join(timeout=...), and
      .result attributes. (FakeWorker in tests; subprocess.Popen wrapped
      with shape from stream_eval.subprocess.run_with_retry_aware_bail
      in production.)

    State machine:
        RUNNING -> PAUSED   (pause(): spawn no new, in-flight finish)
        PAUSED  -> RUNNING  (resume())
        any     -> STOPPED  (stop(): no new spawns, run_until_complete exits
                             on the next poll cycle; in-flight workers are
                             abandoned by the dispatcher but their daemon
                             threads continue until they finish naturally)

    Decrement policy: drain naturally. When target_workers decrements,
    in-flight workers finish their current task; no new workers spawn
    until active < target. No mid-task kill -- if a kill is needed, the
    operator kill -9s the harness and restarts.
    """

    def __init__(self, target_workers, spawn_worker, *, poll_interval=0.05):
        self._target_workers = max(0, int(target_workers))
        self._spawn_worker = spawn_worker
        self._lock = threading.Lock()
        self._state = DispatcherState.RUNNING
        self._poll_interval = poll_interval
        self._pending = deque()
        self._active = []
        self._completed = deque()
        self._completed_event = threading.Event()

    @property
    def target_workers(self):
        with self._lock:
            return self._target_workers

    @target_workers.setter
    def target_workers(self, value):
        with self._lock:
            self._target_workers = max(0, int(value))

    @property
    def state(self):
        with self._lock:
            return self._state

    def submit(self, task):
        """Add a task to the pending queue. Tasks are pulled FIFO."""
        with self._lock:
            self._pending.append(task)

    def run_until_complete(self, timeout=None):
        """Spawn workers up to target_workers, reap finished ones, repeat
        until pending is empty AND active is empty. Returns when the
        pool drains, when state goes to STOPPED, or when timeout
        expires.

        Wall-clock timeout here is a safety backstop; normal completion
        is via `pending empty + active empty`. Pass `timeout=None` (the
        default) for no deadline; positive numeric value sets one. A
        zero/falsy value other than None is also treated as no deadline
        (consistent with the historical behavior of this signature).
        """
        deadline = (time.time() + timeout) if timeout else None
        while True:
            with self._lock:
                if self._state == DispatcherState.STOPPED:
                    break

                # Reap finished workers.
                still_active = []
                for slot in self._active:
                    if slot.worker.is_done():
                        slot.worker.join(timeout=1)
                        if slot.worker.result is not None:
                            self._completed.append(slot.worker.result)
                            self._completed_event.set()
                    else:
                        still_active.append(slot)
                self._active = still_active

                # Spawn up to target_workers (only when RUNNING).
                if self._state == DispatcherState.RUNNING:
                    while (
                        len(self._active) < self._target_workers
                        and self._pending
                    ):
                        task = self._pending.popleft()
                        worker = self._spawn_worker(task)
                        worker.start()
                        self._active.append(WorkerSlot(task, worker))

                done = (
                    not self._pending
                    and not self._active
                    and self._state != DispatcherState.PAUSED
                )
                if done:
                    self._state = DispatcherState.STOPPED
                    break

            if deadline and time.time() >= deadline:
                raise TimeoutError("Dispatcher.run_until_complete timed out")
            time.sleep(self._poll_interval)

    def drain_completed(self):
        """Yield and remove all queued completion records. Safe to call
        from any thread; collects the current snapshot under the lock."""
        with self._lock:
            while self._completed:
                yield self._completed.popleft()
            self._completed_event.clear()

    def pause(self):
        """Stop spawning new workers. In-flight workers continue. To
        truly halt all activity, decrement target_workers to 0 AND pause."""
        with self._lock:
            if self._state == DispatcherState.RUNNING:
                self._state = DispatcherState.PAUSED

    def resume(self):
        """Restart spawning new workers (pre-existing target_workers in
        effect)."""
        with self._lock:
            if self._state == DispatcherState.PAUSED:
                self._state = DispatcherState.RUNNING

    def stop(self):
        """Force the run loop to exit on its next poll cycle.

        Unlike `pause()` (which only blocks new spawns and lets
        `run_until_complete` keep iterating), `stop()` transitions
        directly to STOPPED, breaking the loop. In-flight workers'
        daemon threads continue to completion in the background but
        the dispatcher no longer reaps their results.

        Used by the harness's abort-on-first-timeout path: when a
        retry-budget-exhausted record arrives, we don't want to keep
        spawning new spawns *or* keep iterating the reap loop; we want
        the driver thread to exit so the main thread can finalize.
        """
        with self._lock:
            self._state = DispatcherState.STOPPED
