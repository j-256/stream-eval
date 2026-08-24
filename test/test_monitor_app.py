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


def test_dashboard_renders_poll_inputs_with_env_defaults(client, monkeypatch):
    """Top-right poll-interval inputs read their defaults from env
    vars. Per-tab overrides come from localStorage on the client side
    (not testable from Python), but the server must seed the inputs
    with the env values."""
    monkeypatch.setenv("STREAM_EVAL_POLL_ACTIVE_MS", "2000")
    monkeypatch.setenv("STREAM_EVAL_POLL_IDLE_MS", "15000")
    rv = client.get("/")
    body = rv.data.decode("utf-8")
    assert 'id="poll-active-input"' in body
    assert 'value="2000"' in body
    assert 'id="poll-idle-input"' in body
    assert 'value="15000"' in body


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


def test_workers_stop_route_uses_pid(client):
    """The stop button on a row routes to that row's harness pid socket
    and calls stop() on the client. In-flight runs finish naturally;
    no new spawns happen."""
    with mock.patch(
        "stream_eval.monitor.app.HarnessSocketClient"
    ) as mock_cls:
        instance = mock_cls.return_value
        rv = client.post("/workers/stop/99999")
    assert rv.status_code == 200
    instance.stop.assert_called_once()
    sock_path = mock_cls.call_args[0][0]
    assert sock_path == "/tmp/stream-eval-99999.sock"


def test_active_row_renders_stop_button(client):
    """An active row must surface a stop button; trash must NOT appear
    for active rows (those have to be stopped first)."""
    rv = client.get("/")
    body = rv.data.decode("utf-8")
    assert "open-stop-dialog" in body, "stop button missing on active row"
    assert "open-trash-dialog" not in body, \
        "trash button should not appear on active row"


def test_completed_row_renders_trash_button(tmp_path):
    """A completed row (no active pid) must surface a trash button and
    NOT a stop button. Uses a separate fixture from the module-level
    `client` because that one always pretends pid 99999 is alive."""
    output = tmp_path / "session-y.output"
    output.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=2 workers=2 "
        "total_fixtures=1 pid=88888 ===\n"
        "[1/2] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
        "=== eval finished: kind=trigger skill=dsc-scrape pid=88888 "
        "verdict=completed ===\n"
    )
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[output]), \
         mock.patch("stream_eval.monitor.state._default_is_pid_alive",
                    return_value=False):
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            rv = c.get("/")
    body = rv.data.decode("utf-8")
    assert "open-trash-dialog" in body
    assert "open-stop-dialog" not in body


def test_prune_refuses_when_pid_is_alive(client):
    """The prune route must refuse if the pid is still alive (a running
    harness's tee is still writing to the file). Status 409 with no
    file removed."""
    with mock.patch(
        "stream_eval.monitor.app._is_pid_alive", return_value=True,
    ):
        rv = client.post("/prune/99999")
    assert rv.status_code == 409


def test_prune_removes_dead_pid_output_file(tmp_path, monkeypatch):
    """When the pid is dead, the .output file is moved to trash (or
    unlinked as last resort) and the route returns 200."""
    log_dir = tmp_path / ".claude" / "projects" / "stream-eval"
    log_dir.mkdir(parents=True)
    target = log_dir / "77777.output"
    target.write_text("=== eval starting: ... ===\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[]), \
         mock.patch("stream_eval.monitor.app._is_pid_alive",
                    return_value=False), \
         mock.patch("stream_eval.monitor.app._trash_file") as mock_trash:
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            rv = c.post("/prune/77777")
    assert rv.status_code == 200
    mock_trash.assert_called_once()
    called_path = mock_trash.call_args[0][0]
    assert called_path.name == "77777.output"


def test_prune_idempotent_on_already_missing_file(tmp_path, monkeypatch):
    """If the file's already gone, prune still returns 200 (idempotent).
    No trash call -- nothing to trash."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[]), \
         mock.patch("stream_eval.monitor.app._is_pid_alive",
                    return_value=False), \
         mock.patch("stream_eval.monitor.app._trash_file") as mock_trash:
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            rv = c.post("/prune/55555")
    assert rv.status_code == 200
    mock_trash.assert_not_called()


def test_active_row_renders_start_time_with_date_for_old_run(tmp_path):
    """An active row whose banner stamped started_at must surface that
    timestamp as an inline 'started ...' label, plus a tooltip with the
    full date/time. For a run that did NOT start today, the date is
    inline (HH:MM alone is ambiguous across a multi-day dashboard).
    1780000000.0 is 2026-06-08, well in the past, so the label carries
    the 'Mon DD' date prefix."""
    import time as _time
    output = tmp_path / "session-start.output"
    started_at = 1780000000.0
    output.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=2 workers=2 "
        f"total_fixtures=1 pid=33333 started_at={started_at} ===\n"
        "[1/2] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[output]), \
         mock.patch("stream_eval.monitor.state._default_is_pid_alive",
                    side_effect=lambda pid: pid == 33333):
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            rv = c.get("/")
    body = rv.data.decode("utf-8")
    # Old run -> date-prefixed inline label, e.g. "started Jun 08 00:04".
    expected = _time.strftime("%b %d %H:%M", _time.localtime(started_at))
    assert f"started {expected}" in body, (
        f"expected 'started {expected}' in dashboard body"
    )
    # The full timestamp is still in the tooltip for second-precision.
    full = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(started_at))
    assert full in body


def test_active_row_renders_inline_start_label_end_to_end(tmp_path):
    """End-to-end wiring check: the start_label filter is registered and
    the request's `now` reaches it, so the rendered inline label matches
    _format_start_label for a recent (same-day) timestamp. The compact-
    vs-date branching itself is unit-tested deterministically below; this
    just proves the filter is plumbed through the template with `now`."""
    import time as _time
    from stream_eval.monitor.app import _format_start_label
    now = _time.time()
    started_at = now - 600  # 10 min ago
    output = tmp_path / "session-today.output"
    output.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=2 workers=2 "
        f"total_fixtures=1 pid=33334 started_at={started_at} ===\n"
        "[1/2] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[output]), \
         mock.patch("stream_eval.monitor.state._default_is_pid_alive",
                    side_effect=lambda pid: pid == 33334):
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            rv = c.get("/")
    body = rv.data.decode("utf-8")
    # `now` in the request is captured within milliseconds of our `now`,
    # so the same-day determination is identical.
    expected = _format_start_label(started_at, now=now)
    assert f"started {expected}" in body


def test_completed_row_renders_start_time(tmp_path):
    """Completed rows must also surface started_at so a finished eval's
    'when did this run?' is answerable at a glance, not just 'how
    long did it take?'."""
    import time as _time
    output = tmp_path / "session-start-done.output"
    started_at = 1780000000.0
    finished_at = 1780000600.0
    output.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=1 workers=2 "
        f"total_fixtures=1 pid=22222 started_at={started_at} ===\n"
        "[1/1] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
        "=== eval finished: kind=trigger skill=dsc-scrape pid=22222 "
        f"verdict=completed finished_at={finished_at} ===\n"
    )
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[output]), \
         mock.patch("stream_eval.monitor.state._default_is_pid_alive",
                    return_value=False):
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            rv = c.get("/")
    body = rv.data.decode("utf-8")
    expected = _time.strftime("%b %d %H:%M", _time.localtime(started_at))
    assert f"started {expected}" in body


def test_legacy_row_without_started_at_omits_start_label(tmp_path):
    """Legacy .output files (no started_at in banner) must not render
    a 'started ' label -- there's nothing to render. The runtime
    indicator is also hidden in this case (existing behavior)."""
    output = tmp_path / "session-legacy.output"
    output.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=1 workers=2 "
        "total_fixtures=1 pid=11111 ===\n"
        "[1/1] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[output]), \
         mock.patch("stream_eval.monitor.state._default_is_pid_alive",
                    side_effect=lambda pid: pid == 11111):
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            rv = c.get("/")
    body = rv.data.decode("utf-8")
    assert "started " not in body


def test_active_row_retries_persist_from_completed_runs(tmp_path):
    """Retries from finished runs must keep contributing to the row's
    retry count after the worker that did them exits. The live-worker
    tally drops to 0 the instant a retrying worker finishes (and is
    always 0 for trigger evals, whose tempfile transcripts can't be
    read), so the header must sum the per-cell retries carried on each
    progress line. Two completed runs with retries=2 and retries=1
    here, no live workers -> the header shows '3 retries'."""
    output = tmp_path / "session-retries.output"
    output.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=3 workers=2 "
        "total_fixtures=1 pid=44440 started_at=1780000000.0 ===\n"
        "[1/3] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=2 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
        "[2/3] kind=trigger pass=True fixture_id=q0 run=2 elapsed=5s "
        "retries=1 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    # No live workers (find_agent_workers_for returns nothing), pid
    # alive so the row is active.
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[output]), \
         mock.patch("stream_eval.monitor.app.find_agent_workers_for",
                    return_value=[]), \
         mock.patch("stream_eval.monitor.state._default_is_pid_alive",
                    side_effect=lambda pid: pid == 44440):
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            rv = c.get("/")
    body = rv.data.decode("utf-8")
    assert "3 retries" in body, "cumulative retries from cells not shown"


def test_completed_row_surfaces_cumulative_retries(tmp_path):
    """A finished run that fought throttling keeps its retry count
    visible after completion -- the operator can see at a glance which
    historical runs were retry-heavy."""
    output = tmp_path / "session-done-retries.output"
    output.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=2 workers=2 "
        "total_fixtures=1 pid=44441 started_at=1780000000.0 ===\n"
        "[1/2] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=4 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
        "[2/2] kind=trigger pass=True fixture_id=q0 run=2 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
        "=== eval finished: kind=trigger skill=dsc-scrape pid=44441 "
        "verdict=completed finished_at=1780000600.0 ===\n"
    )
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[output]), \
         mock.patch("stream_eval.monitor.state._default_is_pid_alive",
                    return_value=False):
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            rv = c.get("/")
    body = rv.data.decode("utf-8")
    assert "4 retries" in body


def test_zero_retry_active_row_omits_retry_count(tmp_path):
    """A clean run (no retries anywhere) shows just 'N in flight' with
    no ', 0 retries' tail -- the count only appears when it's nonzero,
    to keep the header uncluttered in the common case.

    Scoped to the header's in-flight-stat span: the recent-completions
    table legitimately carries a 'retries' column header, so a
    body-wide substring check would false-positive on it."""
    import re
    output = tmp_path / "session-clean.output"
    output.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=2 workers=2 "
        "total_fixtures=1 pid=44442 started_at=1780000000.0 ===\n"
        "[1/2] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[output]), \
         mock.patch("stream_eval.monitor.app.find_agent_workers_for",
                    return_value=[]), \
         mock.patch("stream_eval.monitor.state._default_is_pid_alive",
                    side_effect=lambda pid: pid == 44442):
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            rv = c.get("/")
    body = rv.data.decode("utf-8")
    stat = re.search(r'<span class="in-flight-stat".*?</span>', body, re.S)
    assert stat, "in-flight-stat span not rendered for active row"
    assert "in flight" in stat.group(0)
    assert "retries" not in stat.group(0)


def test_format_start_label_same_day_is_compact():
    """_format_start_label returns bare HH:MM when start and now fall on
    the same local calendar day."""
    import time as _time
    from stream_eval.monitor.app import _format_start_label
    start = 1780000000.0
    # `now` two hours after start, same day.
    now = start + 2 * 3600
    label = _format_start_label(start, now=now)
    assert label == _time.strftime("%H:%M", _time.localtime(start))


def test_format_start_label_different_day_includes_date():
    """_format_start_label prefixes the month/day when the run started
    on a different calendar day than now -- disambiguating a multi-day
    dashboard without relying on the hover tooltip."""
    import time as _time
    from stream_eval.monitor.app import _format_start_label
    start = 1780000000.0
    # `now` three days later.
    now = start + 3 * 86400
    label = _format_start_label(start, now=now)
    assert label == _time.strftime("%b %d %H:%M", _time.localtime(start))


def test_format_start_label_none_is_empty():
    from stream_eval.monitor.app import _format_start_label
    assert _format_start_label(None) == ""


def test_recent_table_shows_per_run_retries(tmp_path):
    """The Recent completions table carries a retries column so the
    operator can see which specific runs fought throttling, not just
    the row-level cumulative total. A retry-heavy run shows its count;
    a clean run shows '-'."""
    import re
    output = tmp_path / "session-recent-retries.output"
    output.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=2 workers=2 "
        "total_fixtures=1 pid=66660 started_at=1780000000.0 ===\n"
        "[1/2] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=4 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
        "[2/2] kind=trigger pass=False fixture_id=q0 run=2 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
        "=== eval finished: kind=trigger skill=dsc-scrape pid=66660 "
        "verdict=completed finished_at=1780000600.0 ===\n"
    )
    with mock.patch("stream_eval.monitor.app.find_output_files",
                    return_value=[output]), \
         mock.patch("stream_eval.monitor.state._default_is_pid_alive",
                    return_value=False):
        app = create_app(session=None)
        app.testing = True
        with app.test_client() as c:
            rv = c.get("/")
    body = rv.data.decode("utf-8")
    recent = re.search(
        r'<details class="row-recent".*?</details>', body, re.S,
    )
    assert recent, "recent-completions table not rendered"
    section = recent.group(0)
    assert "<th>retries</th>" in section
    # The retry-heavy run's count appears; the clean run shows a dash.
    assert "<td>4</td>" in section
    assert "<td>-</td>" in section
