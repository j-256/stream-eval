"""Retry-aware subprocess wrapper for the eval harnesses.

The Claude CLI emits stream-json events of the shape

  {"type":"system","subtype":"api_retry","attempt":N,"max_retries":M,
   "error":"rate_limit"|"server_error",...}

while waiting on the upstream API. This module wraps `subprocess.Popen`
to provide three independent bail signals:

  - api_retry budget exhausted (`attempt == max_retries` on the most
    recent retry event). The principled "upstream poisoned" signal.
  - Retry-aware wall clock: `time.time() - t0 - time_in_retries >
    timeout`. Measures effective model-thinking time only -- backoff
    waits do not count.
  - Absolute wall clock at `4 * timeout`: catches the truly-stuck case
    where the process is wedged inside a retry sleep and emits no
    further events. Surfaces as `wall_timed_out_in_retry=True`.

Both `stream_eval.trigger` and `stream_eval.synthesis` use this so the
bail semantics stay consistent across the two harnesses.

The retry-window state machine lives on `RetryClock` (testable in
isolation with an injectable `now_fn`); `run_with_retry_aware_bail` is
the file/process glue around it.
"""
import json
import subprocess
import time


POLL_INTERVAL_S = 0.5

ABSOLUTE_BACKSTOP_MULTIPLIER = 4


def classify_line(d):
    """Classify a parsed stream-json event for the retry-window state
    machine.

    Returns:
      ('retry', {attempt, max_retries}) for api_retry events.
      ('output', None) for events that bear model output (assistant
        content, tool_use, result). These close any in-flight retry
        window because the model has resumed producing.
      (None, None) for everything else (init, hook_response, anything
        we don't model).
    """
    if not isinstance(d, dict):
        return None, None
    t = d.get("type")
    if t == "system" and d.get("subtype") == "api_retry":
        return "retry", {
            "attempt": d.get("attempt", 0),
            "max_retries": d.get("max_retries", 0),
        }
    if t in ("assistant", "user", "result"):
        return "output", None
    return None, None


class RetryClock:
    """State machine that tracks how much wall-clock time was spent
    inside CLI retry-backoff windows.

    States:
      idle      : not currently waiting on a retry.
      in_retry  : an api_retry event has been seen and we're waiting
                  for the next output-bearing event (or process exit).

    Transitions:
      idle    --retry-->     in_retry   (retry_started_at = now)
      in_retry --retry-->    in_retry   (no clock change; contiguous
                                         retry continues)
      in_retry --output-->   idle       (close window: time_in_retries
                                         += now - retry_started_at)
      in_retry --close-->    idle       (used by the wrapper on process
                                         exit / kill mid-retry)

    `now_fn` is injected for testability; defaults to time.time.
    `t0` is captured at construction (or supplied) so the same clock
    can answer effective_elapsed() vs absolute_elapsed().
    """
    def __init__(self, now_fn=None, t0=None):
        self._now_fn = now_fn or time.time
        self._t0 = t0 if t0 is not None else self._now_fn()
        self._retry_started_at = None
        self.time_in_retries = 0.0

    @property
    def in_retry(self):
        return self._retry_started_at is not None

    def on_event(self, kind, info):
        """Advance the state machine on a classified event. `kind` is
        the first element of classify_line's return; `info` is the
        second (unused for output events)."""
        if kind == "retry":
            if self._retry_started_at is None:
                self._retry_started_at = self._now_fn()
        elif kind == "output":
            self.close_open_window()

    def close_open_window(self):
        """Force the in-flight retry window closed (no-op if idle).
        The wrapper calls this when the process exits while still
        in_retry, so time_in_retries accounting stays correct for
        diagnostics."""
        if self._retry_started_at is not None:
            self.time_in_retries += self._now_fn() - self._retry_started_at
            self._retry_started_at = None

    def effective_elapsed(self):
        """Wall-clock seconds since t0, minus retry-backoff time. If a
        retry window is currently open, the in-flight portion is also
        excluded (otherwise the wall-clock check could fire while
        we're legitimately waiting on backoff)."""
        elapsed = self._now_fn() - self._t0
        in_flight = (
            self._now_fn() - self._retry_started_at
            if self._retry_started_at is not None
            else 0.0
        )
        return elapsed - self.time_in_retries - in_flight

    def absolute_elapsed(self):
        """Wall-clock seconds since t0, including all retry time. Used
        for the absolute backstop that catches stuck-during-retry
        processes."""
        return self._now_fn() - self._t0


def run_with_retry_aware_bail(cmd, stdout_path, env, cwd, timeout):
    """Spawn `cmd`, redirect stdout to `stdout_path`, and stream the
    JSONL there live to detect retry-budget exhaustion.

    Bail conditions, in priority order:
    1. CLI exhausted its retry budget (api_retry attempt == max_retries
       on the most recent retry event). Returns retry_budget_exhausted=True.
    2. Retry-aware wall clock exceeded `timeout` (model-thinking time,
       excluding retry-backoff). Returns wall_timed_out=True.
    3. Absolute wall clock exceeded `ABSOLUTE_BACKSTOP_MULTIPLIER *
       timeout`. Catches the case where the process is wedged inside a
       retry sleep and emits nothing further -- the retry-aware clock
       would never advance there. Returns wall_timed_out_in_retry=True.

    The caller opens stdout_path themselves before calling, and is
    responsible for parsing the file's contents after the call returns.

    The caller controls the spawn's full environment via `env`. Specifically:
    to give the spawn an isolated HOME, override env["HOME"] before calling.
    The harness-side helper that builds such an env is
    stream_eval.isolation.prepare_isolated_home.

    Returns a dict:
      {
        "retry_budget_exhausted": bool,
        "wall_timed_out": bool,
        "wall_timed_out_in_retry": bool,
        "total_retries": int,    # cumulative across all api_retry events seen
        "latest_attempt": int,   # attempt N on the most recent retry event
        "max_retries_field": int,
        "time_in_retries": float, # seconds spent in retry-backoff windows
        "exit_code": int | None, # process exit code, or None if killed
      }
    """
    retry_budget_exhausted = False
    wall_timed_out = False
    wall_timed_out_in_retry = False
    total_retries = 0
    latest_attempt = 0
    max_retries_field = 0

    with open(stdout_path, "w") as out_f:
        proc = subprocess.Popen(cmd, stdout=out_f, stderr=subprocess.DEVNULL,
                                env=env, cwd=cwd)
        clock = RetryClock()
        read_pos = 0

        while True:
            with open(stdout_path) as in_f:
                in_f.seek(read_pos)
                chunk = in_f.read()
                read_pos = in_f.tell()
            if chunk:
                for raw in chunk.splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    kind, info = classify_line(d)
                    clock.on_event(kind, info)
                    if kind == "retry":
                        total_retries += 1
                        latest_attempt = info["attempt"]
                        if info["max_retries"] > max_retries_field:
                            max_retries_field = info["max_retries"]
                        if info["max_retries"] and info["attempt"] >= info["max_retries"]:
                            retry_budget_exhausted = True
                            break
            if retry_budget_exhausted:
                break
            if proc.poll() is not None:
                break
            if clock.effective_elapsed() > timeout:
                wall_timed_out = True
                break
            if clock.absolute_elapsed() > timeout * ABSOLUTE_BACKSTOP_MULTIPLIER:
                wall_timed_out_in_retry = True
                break
            time.sleep(POLL_INTERVAL_S)

        if retry_budget_exhausted or wall_timed_out or wall_timed_out_in_retry:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

        clock.close_open_window()

    return {
        "retry_budget_exhausted": retry_budget_exhausted,
        "wall_timed_out": wall_timed_out,
        "wall_timed_out_in_retry": wall_timed_out_in_retry,
        "total_retries": total_retries,
        "latest_attempt": latest_attempt,
        "max_retries_field": max_retries_field,
        "time_in_retries": clock.time_in_retries,
        "exit_code": proc.returncode,
    }
