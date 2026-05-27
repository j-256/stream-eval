"""Tests for stream_eval.monitor.app: Flask routes."""
import os
from unittest import mock

import pytest

from stream_eval.monitor.app import create_app


@pytest.fixture
def client(tmp_path):
    """Flask test client. Mock find_output_files to point at tmp_path."""
    output = tmp_path / "session-x.output"
    output.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=2 workers=2 "
        "total_fixtures=1 ===\n"
        "[1/2] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[output]):
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


def test_workers_increment_route_calls_socket_client(client):
    with mock.patch(
        "stream_eval.monitor.app.HarnessSocketClient"
    ) as mock_cls:
        instance = mock_cls.return_value
        rv = client.post("/workers/+1")
    assert rv.status_code == 200
    body = rv.data.decode("utf-8")
    assert "<!doctype html>" not in body.lower()  # partial only
    instance.increment.assert_called_once()


def test_workers_decrement_route(client):
    with mock.patch(
        "stream_eval.monitor.app.HarnessSocketClient"
    ) as mock_cls:
        instance = mock_cls.return_value
        rv = client.post("/workers/-1")
    assert rv.status_code == 200
    instance.decrement.assert_called_once()


def test_workers_pause_resume_routes(client):
    with mock.patch(
        "stream_eval.monitor.app.HarnessSocketClient"
    ) as mock_cls:
        instance = mock_cls.return_value
        rv1 = client.post("/workers/pause")
        rv2 = client.post("/workers/resume")
    assert rv1.status_code == 200
    assert rv2.status_code == 200
    instance.pause.assert_called_once()
    instance.resume.assert_called_once()
