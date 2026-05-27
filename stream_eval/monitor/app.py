"""Flask app for the live dashboard.

Read-only dashboard renders DashboardState. Worker-control routes
(`POST /workers/<action>/<pid>`) talk to a specific harness over its
Unix socket -- per-row routing means a +1 button on the dsc-scrape
row never affects a concurrently-running dsc-triage eval.

Public surface:
- create_app(session): Flask application factory; the test client
  uses this.
- run_app(host, port, session, auto_open): production entry point;
  starts the Flask development server.
- print_summary(session): one-shot CLI summary (replaces the old
  no-arg `eval-monitor.py`).
"""
import os
import time
import webbrowser

from flask import (
    Flask, render_template, request,
)

from stream_eval.monitor.ps import (
    detect_session,
    find_claude_workers_for,
    find_output_files,
)
from stream_eval.monitor.socket_client import (
    HarnessSocketClient, SocketClientError,
)
from stream_eval.monitor.state import build_state


def create_app(session=None):
    app = Flask(__name__)

    def _resolve_session():
        return session or detect_session()

    def _gather_state():
        state = build_state(find_output_files())
        for row in state.rows:
            row.target_workers, row.dispatcher_state = _row_socket_snapshot(row)
            row.workers_for_row = _claude_workers_for_row(row)
            row.recent = _recent_for_row(row)
            row.in_flight_count = len(row.workers_for_row)
            row.in_flight_retries = sum(
                w.get("retries", 0) for w in row.workers_for_row
            )
        return {
            "state": state,
            "session": _resolve_session(),
            "poll_active_ms": _poll_default("STREAM_EVAL_POLL_ACTIVE_MS", 5000),
            "poll_idle_ms": _poll_default("STREAM_EVAL_POLL_IDLE_MS", 30000),
        }

    @app.route("/", methods=["GET"])
    def dashboard():
        ctx = _gather_state()
        if request.args.get("_partial") == "1":
            return render_template("partials/_dashboard_main.html", **ctx)
        return render_template("dashboard.html", **ctx)

    def _action_response_for_pid(pid, action):
        """Apply `action` to the socket of the harness identified by
        `pid`, then re-render the partial. Per-row pid lets us address
        exactly one harness even when several are running concurrently;
        the `youngest socket` heuristic the legacy app used would route
        to whichever started most recently regardless of which row's
        button was clicked."""
        sock_path = f"/tmp/stream-eval-{pid}.sock"
        client = HarnessSocketClient(sock_path)
        try:
            action(client)
        except SocketClientError:
            pass
        ctx = _gather_state()
        return render_template("partials/_dashboard_main.html", **ctx)

    @app.route("/workers/+1/<int:pid>", methods=["POST"],
               endpoint="workers_increment")
    def workers_increment(pid):
        return _action_response_for_pid(pid, lambda c: c.increment())

    @app.route("/workers/-1/<int:pid>", methods=["POST"],
               endpoint="workers_decrement")
    def workers_decrement(pid):
        return _action_response_for_pid(pid, lambda c: c.decrement())

    @app.route("/workers/pause/<int:pid>", methods=["POST"],
               endpoint="workers_pause")
    def workers_pause(pid):
        return _action_response_for_pid(pid, lambda c: c.pause())

    @app.route("/workers/resume/<int:pid>", methods=["POST"],
               endpoint="workers_resume")
    def workers_resume(pid):
        return _action_response_for_pid(pid, lambda c: c.resume())

    return app


def _row_socket_snapshot(row):
    """Read both the target_workers count AND the dispatcher state
    (running / paused) from this row's harness socket. Returns
    (target_workers, dispatcher_state); both None for non-active rows
    or when the socket isn't reachable.

    Reading both in one call lets the dashboard show that pause
    actually took effect -- the previous snapshot only read workers,
    so a successful PAUSE made no visible change to the UI."""
    if row.status != "active" or row.harness_pid is None:
        return (None, None)
    sock_path = f"/tmp/stream-eval-{row.harness_pid}.sock"
    client = HarnessSocketClient(sock_path)
    try:
        workers = client.get_workers()
    except SocketClientError:
        return (None, None)
    try:
        state = client.get_state()
    except SocketClientError:
        state = None
    return (workers, state)


def _claude_workers_for_row(row):
    """Return [{pid, started_at_human, cmdline}, ...] for the live
    `claude -p` children of this row's harness pid. Empty list for
    completed/aborted/unknown rows or when the harness has no children
    at this poll instant."""
    if row.status != "active" or row.harness_pid is None:
        return []
    out = []
    for w in find_claude_workers_for(row.harness_pid):
        w["started_at_human"] = _humanize(time.time() - w["started_at"])
        out.append(w)
    return out


def _recent_for_row(row):
    """Last 5 cells for this row, rendered as recent-completion records.

    DashboardCell only carries (fixture_id, run, pass_, contaminated)
    today. The runner's progress line carries more
    (elapsed/retries/first_tool/first_skill/asserts/query) but state.py
    discards them after computing pass/fail. A follow-up could plumb
    them through DashboardCell so the recent-completions table can
    show the same depth as the original eval-monitor."""
    out = []
    for cell in reversed(row.cells[-5:]):
        out.append({
            "fixture_id": cell.fixture_id,
            "run": cell.run,
            "pass_": cell.pass_ if cell.pass_ is not None else False,
            "contaminated": cell.contaminated,
        })
    return out


def _poll_default(env_var, fallback):
    """Read an integer ms from env, falling back to a hardcoded
    default. The frontend uses this as the seed value for its
    in-page tuning inputs; per-tab overrides come from localStorage."""
    raw = os.environ.get(env_var)
    if raw is None:
        return fallback
    try:
        return max(100, int(raw))  # 100ms floor: don't let env spam the harness
    except ValueError:
        return fallback


def _humanize(seconds):
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    return f"{int(seconds / 3600)}h ago"


def run_app(*, host, port, session, auto_open):
    app = create_app(session=session)
    if auto_open:
        webbrowser.open(f"http://{host}:{port}/")
    app.run(host=host, port=port, debug=False)
    return 0


def print_summary(session=None):
    state = build_state(find_output_files())
    sid = session or detect_session()
    if sid:
        print(f"session: {sid}")
    for row in state.rows:
        passed = sum(1 for c in row.cells if c.pass_ is True)
        total = row.total_fixtures * row.runs
        pid_label = f" pid={row.harness_pid}" if row.harness_pid else ""
        print(
            f"  [{row.status}] {row.skill} ({row.kind}){pid_label}: "
            f"{passed}/{total}"
        )
    return 0
