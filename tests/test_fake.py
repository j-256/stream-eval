"""Tests for stream_eval.fake: scenario builders + socket server.

Each scenario must produce parseable .output files that drive the
real build_state into the dashboard rows the scenario name implies.
This is the test that catches drift between the runner's emitted
format and the fake's synthesized one.
"""
import os
import socket as _socket
import uuid

from stream_eval.fake import SCENARIOS, make_fake_state
from stream_eval.fake.socket_server import FakeSocketServer
from stream_eval.monitor.state import build_state


def _short_sock_path():
    """AF_UNIX paths max out at ~104 bytes on macOS / ~108 on Linux.
    pytest's tmp_path gives us 80+ bytes of test-name prefix before we
    even append a filename, which blows past the limit on macOS. Use
    /tmp directly with a uuid suffix so the path stays well inside."""
    return f"/tmp/stream-eval-faketest-{uuid.uuid4().hex[:8]}.sock"


def _build(state):
    return build_state(
        state.output_paths,
        is_pid_alive=state.is_pid_alive,
    )


def test_scenarios_registry_is_complete():
    """The list of scenarios is part of the public surface; this
    test catches accidental removal or rename."""
    expected = {
        "active-clean", "active-with-failures",
        "active-with-contamination", "concurrent",
        "completed", "aborted", "aborted-no-finish-banner",
        "legacy", "over-cap", "full-spread",
    }
    assert set(SCENARIOS) == expected


def test_active_clean_renders_one_active_row():
    with make_fake_state("active-clean") as state:
        ds = _build(state)
        assert len(ds.rows) == 1
        row = ds.rows[0]
        assert row.status == "active"
        assert row.skill == "dsc-scrape"
        assert len([c for c in row.cells if c.pass_]) == 3


def test_active_with_failures_has_red_cells():
    with make_fake_state("active-with-failures") as state:
        ds = _build(state)
        row = ds.rows[0]
        assert any(c.pass_ is False for c in row.cells)
        assert any(c.pass_ is True for c in row.cells)


def test_active_with_contamination_marks_cells():
    with make_fake_state("active-with-contamination") as state:
        ds = _build(state)
        row = ds.rows[0]
        assert any(c.contaminated for c in row.cells)


def test_concurrent_produces_two_active_rows():
    """The routing-bug scenario: distinct harness pids, distinct
    skills, both active. The dashboard's per-row controls must be
    independently addressable."""
    with make_fake_state("concurrent") as state:
        ds = _build(state)
        assert len(ds.rows) == 2
        assert all(r.status == "active" for r in ds.rows)
        pids = {r.harness_pid for r in ds.rows}
        assert len(pids) == 2


def test_completed_status_from_finish_banner():
    with make_fake_state("completed") as state:
        ds = _build(state)
        assert ds.rows[0].status == "completed"


def test_aborted_status_from_finish_banner():
    with make_fake_state("aborted") as state:
        ds = _build(state)
        assert ds.rows[0].status == "aborted"


def test_aborted_no_finish_banner_falls_through_to_pid_liveness():
    """No finish banner -> pid liveness probe decides. The same
    .output file renders as 'active' when the predicate says the
    fake harness pid is alive, and 'aborted' when it says not.
    The runner-crashed-before-finish-banner case is exactly this
    state."""
    with make_fake_state("aborted-no-finish-banner") as state:
        # Default predicate: every fake pid is "alive" -> active.
        ds_alive = _build(state)
        assert ds_alive.rows[0].status == "active"
        # Hostile predicate that always says no -> aborted.
        ds_dead = build_state(
            state.output_paths,
            is_pid_alive=lambda pid: False,
        )
        assert ds_dead.rows[0].status == "aborted"


def test_legacy_status_unknown():
    with make_fake_state("legacy") as state:
        ds = _build(state)
        assert ds.rows[0].status == "unknown"
        assert ds.rows[0].harness_pid is None


def test_over_cap_drops_oldest_completed_rows():
    """over-cap synthesizes 7 completed rows of one skill at
    progressively older mtimes. Per-skill cap of 5 should keep the
    youngest 5."""
    with make_fake_state("over-cap") as state:
        ds = build_state(
            state.output_paths,
            is_pid_alive=state.is_pid_alive,
            per_skill_cap=5,
        )
        assert len(ds.rows) == 5
        assert all(r.status == "completed" for r in ds.rows)


def test_full_spread_has_one_of_each_state():
    """The visual smoke benchmark scenario must produce at least
    one row of each status state machine value (except 'completed'
    which can be absent if no scenario builder uses it -- full-spread
    intentionally includes one). Active rows always have the live
    socket; legacy rows always have harness_pid=None."""
    with make_fake_state("full-spread") as state:
        ds = _build(state)
        statuses = {r.status for r in ds.rows}
        assert "active" in statuses
        assert "completed" in statuses
        assert "aborted" in statuses
        assert "unknown" in statuses


# ---------- socket server ----------

def test_fake_socket_server_responds_to_get_workers():
    sock_path = _short_sock_path()
    server = FakeSocketServer(sock_path, initial_workers=4)
    try:
        resp = _send(sock_path, "GET workers")
        assert resp == "4"
    finally:
        server.close()


def test_fake_socket_server_set_workers_persists():
    """SET workers mutates the in-memory dispatcher; subsequent
    GET workers reflects the new value -- exactly the behavior the
    +1 dashboard button relies on."""
    sock_path = _short_sock_path()
    server = FakeSocketServer(sock_path, initial_workers=2)
    try:
        assert _send(sock_path, "SET workers 7") == "OK"
        assert _send(sock_path, "GET workers") == "7"
    finally:
        server.close()


def test_fake_socket_server_pause_resume():
    sock_path = _short_sock_path()
    server = FakeSocketServer(sock_path)
    try:
        assert _send(sock_path, "PAUSE") == "OK"
        assert _send(sock_path, "GET state") == "paused"
        assert _send(sock_path, "RESUME") == "OK"
        assert _send(sock_path, "GET state") == "running"
    finally:
        server.close()


def test_fake_socket_server_close_unlinks_socket():
    sock_path = _short_sock_path()
    server = FakeSocketServer(sock_path)
    assert os.path.exists(sock_path)
    server.close()
    assert not os.path.exists(sock_path)


def _send(sock_path, command, timeout=2.0):
    """Helper: open a connection, send one command, read one
    response, close. Mirrors HarnessSocketClient's per-call shape."""
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall((command + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8").strip()
    finally:
        s.close()
