"""Tests for stream_eval.monitor.app: Flask routes.

The app's worker-control routes take a per-row harness pid so a button
on one eval's row never affects another concurrently-running eval.
The fixture pretends pid 99999 is alive so the row gets status='active'
and renders controls.
"""
from unittest import mock

import pytest

from stream_eval.monitor.app import create_app


@pytest.fixture
def client(tmp_path):
    """Flask test client backed by a single 'active' .output file."""
    output = tmp_path / "session-x.output"
    output.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=2 workers=2 "
        "total_fixtures=1 pid=99999 ===\n"
        "[1/2] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    # Force the row to active by stubbing the liveness check used inside
    # build_state. The stub returns True for pid 99999 so DashboardRow's
    # status comes out 'active' and the per-row controls render.
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[output]), \
         mock.patch("stream_eval.monitor.state._default_is_pid_alive",
                    side_effect=lambda pid: pid == 99999):
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            yield c


def test_dashboard_get_returns_html_with_skill_row(client):
    rv = client.get("/")
    assert rv.status_code == 200
    body = rv.data.decode("utf-8")
    assert "dsc-scrape" in body
    assert "trigger" in body


def test_dashboard_partial_returns_main_only(client):
    rv = client.get("/?_partial=1")
    assert rv.status_code == 200
    body = rv.data.decode("utf-8")
    # Partial response should NOT contain the full HTML envelope.
    assert "<!doctype html>" not in body.lower()
    # But it should contain the row content.
    assert "dsc-scrape" in body


def test_dashboard_renders_status_badge_for_active_row(client):
    """An active row's status field must surface as 'active' in the HTML
    so the operator can tell it's mid-run vs completed/aborted."""
    rv = client.get("/")
    body = rv.data.decode("utf-8")
    assert "status-active" in body
    assert "pid 99999" in body


def test_workers_increment_route_calls_socket_for_pid(client):
    """The +1 button on a row routes to that row's harness pid socket."""
    with mock.patch(
        "stream_eval.monitor.app.HarnessSocketClient"
    ) as mock_cls:
        instance = mock_cls.return_value
        rv = client.post("/workers/+1/99999")
    assert rv.status_code == 200
    instance.increment.assert_called_once()
    # The first positional arg to HarnessSocketClient is the socket path
    # for the addressed pid.
    sock_path = mock_cls.call_args[0][0]
    assert sock_path == "/tmp/stream-eval-99999.sock"


def test_workers_decrement_route_uses_pid(client):
    with mock.patch(
        "stream_eval.monitor.app.HarnessSocketClient"
    ) as mock_cls:
        instance = mock_cls.return_value
        rv = client.post("/workers/-1/99999")
    assert rv.status_code == 200
    instance.decrement.assert_called_once()


def test_workers_pause_resume_routes_use_pid(client):
    with mock.patch(
        "stream_eval.monitor.app.HarnessSocketClient"
    ) as mock_cls:
        instance = mock_cls.return_value
        rv1 = client.post("/workers/pause/99999")
        rv2 = client.post("/workers/resume/99999")
    assert rv1.status_code == 200
    assert rv2.status_code == 200
    instance.pause.assert_called_once()
    instance.resume.assert_called_once()


def test_workers_route_rejects_non_integer_pid(client):
    """Flask's <int:pid> converter 404s on non-numeric values, which
    is fine -- it's belt-and-suspenders against malformed routes."""
    rv = client.post("/workers/+1/notanumber")
    assert rv.status_code == 404


def test_dashboard_renders_dispatcher_state_badge(client):
    """Active rows must show running / paused badges so a successful
    PAUSE click produces a visible UI change. Without this, pause
    'works' in the protocol but is invisible to the operator."""
    with mock.patch(
        "stream_eval.monitor.app.HarnessSocketClient"
    ) as mock_cls:
        instance = mock_cls.return_value
        instance.get_workers.return_value = 4
        instance.get_state.return_value = "paused"
        rv = client.get("/")
    body = rv.data.decode("utf-8")
    assert "dispatcher-state paused" in body


def test_socket_client_failure_does_not_500_the_dashboard(client):
    """If the harness has already exited, /tmp/stream-eval-<pid>.sock is
    missing; the route must absorb SocketClientError and still render
    the partial."""
    from stream_eval.monitor.socket_client import SocketClientError
    with mock.patch(
        "stream_eval.monitor.app.HarnessSocketClient"
    ) as mock_cls:
        instance = mock_cls.return_value
        instance.increment.side_effect = SocketClientError("no socket")
        rv = client.post("/workers/+1/99999")
    assert rv.status_code == 200
