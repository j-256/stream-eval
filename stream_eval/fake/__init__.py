"""Fake harnesses + scenario presets for dashboard development.

The dashboard renders state derived from .output files (parsed via
stream_eval.monitor.state) and live AF_UNIX sockets at
/tmp/stream-eval-<pid>.sock (talked to via HarnessSocketClient). In
real operation those come from running trigger/synthesis evals -- but
exercising the dashboard against a real eval costs API tokens and
takes minutes per scenario.

This submodule synthesizes both layers:
- Pre-baked .output files for every state the dashboard handles
  (active, completed, aborted, contaminated, legacy, etc.).
- Stateful in-memory listeners on /tmp/stream-eval-<fake-pid>.sock
  that respond to GET/SET workers, PAUSE, RESUME -- so clicking the
  +1 button on the dashboard visibly mutates the displayed count
  without there being any real dispatcher behind it.

Public surface:
- make_fake_state(scenario, *, base_dir=None): synthesize one
  scenario's outputs and sockets. Returns FakeState (close() to
  clean up).
- FakeState: holds the writable temp dir, the running listener
  threads, and tear-down logic (delete .output, unlink sockets,
  signal listeners to stop).
- SCENARIOS: dict of scenario name -> builder callable.

Run interactively:
    python3 -m stream_eval.fake <scenario>

The fake has no relationship to real harnesses: it does not spawn an
agent CLI, it does not invoke run_eval, and it never writes results.json.
The fake pids are arbitrary integers chosen to not collide with
likely real pids on the host (>= 90000) but the fake socket files
live next to real ones in /tmp -- closing FakeState removes them.
"""
from stream_eval.fake.runs import (
    FakeState,
    make_fake_state,
    SCENARIOS,
)

__all__ = ["FakeState", "make_fake_state", "SCENARIOS"]
