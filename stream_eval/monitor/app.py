"""Flask app for the live dashboard.

Read-only dashboard renders DashboardState. Worker-control routes
(`POST /workers/...`) talk to the harness over a Unix socket.

Public surface:
- create_app(session): Flask application factory; the test client
  uses this.
- run_app(host, port, session, auto_open): production entry point;
  starts the Flask development server.
- print_summary(session): one-shot CLI summary (replaces the old
  no-arg `eval-monitor.py`).
"""
import glob
import os
import time
import webbrowser

from flask import (
    Flask, render_template, request,
)

from stream_eval.monitor.ps import (
    detect_session,
    find_eval_workers,
    find_output_files,
)
from stream_eval.monitor.socket_client import (
    HarnessSocketClient, SocketClientError,
)
from stream_eval.monitor.state import build_state

# Default socket path used when no specific path is found.
_DEFAULT_SOCK_PATH = "/tmp/stream-eval-dashboard.sock"


def create_app(session=None):
    app = Flask(__name__)

    def _resolve_session():
        return session or detect_session()

    def _gather_state():
        state = build_state(find_output_files())
        active = list(find_eval_workers())
        for w in active:
            w["started_at_human"] = _humanize(time.time() - w["started_at"])
        # Synthesize 'recent' from the last few cells across all rows.
        # The real recent table comes from per-run elapsed/retries/query
        # which DashboardCell doesn't carry today. F-iteration follow-up
        # could enrich this; for now the recent table shows minimal info.
        recent = []
        for row in state.rows:
            for cell in reversed(row.cells[-5:]):
                recent.append({
                    "fixture_id": cell.fixture_id,
                    "run": cell.run,
                    "pass_": cell.pass_ if cell.pass_ is not None else False,
                    "elapsed": 0.0,
                    "retries": 0,
                    "query": "",
                })
        target_workers = _try_get_target_workers()
        return {
            "state": state,
            "active_workers": active,
            "recent": recent,
            "session": _resolve_session(),
            "target_workers": target_workers,
        }

    @app.route("/", methods=["GET"])
    def dashboard():
        ctx = _gather_state()
        if request.args.get("_partial") == "1":
            return render_template("partials/_dashboard_main.html", **ctx)
        return render_template("dashboard.html", **ctx)

    def _action_response(action):
        """Apply `action` to the harness socket, then render the
        partial dashboard with the updated state. Returning the
        partial inline (rather than 302-redirecting) means the UI
        reflects the new target_workers / state immediately, without
        waiting for the next poll cycle."""
        _with_socket(action)
        ctx = _gather_state()
        return render_template("partials/_dashboard_main.html", **ctx)

    @app.route("/workers/+1", methods=["POST"], endpoint="workers_increment")
    def workers_increment():
        return _action_response(lambda c: c.increment())

    @app.route("/workers/-1", methods=["POST"], endpoint="workers_decrement")
    def workers_decrement():
        return _action_response(lambda c: c.decrement())

    @app.route("/workers/pause", methods=["POST"], endpoint="workers_pause")
    def workers_pause():
        return _action_response(lambda c: c.pause())

    @app.route("/workers/resume", methods=["POST"], endpoint="workers_resume")
    def workers_resume():
        return _action_response(lambda c: c.resume())

    return app


def _with_socket(action):
    """Locate the most-recent harness socket, construct a
    HarnessSocketClient, and apply `action` to it. Silently absorbs
    SocketClientError so a 'no harness running' state doesn't 500 the
    dashboard.

    We always instantiate HarnessSocketClient (rather than skipping when
    no socket file exists) so that test patches on HarnessSocketClient
    take effect -- the mock's __init__ succeeds even if the path is fake,
    and SocketClientError from _send is what we absorb in production.
    """
    sock_path = _find_harness_socket() or _DEFAULT_SOCK_PATH
    client = HarnessSocketClient(sock_path)
    try:
        action(client)
    except SocketClientError:
        pass


def _find_harness_socket():
    """Find the youngest /tmp/stream-eval-*.sock. Returns the path or
    None. If multiple harnesses are running, the youngest is the one
    the user most recently started -- a heuristic, not a contract."""
    candidates = glob.glob("/tmp/stream-eval-*.sock")
    if not candidates:
        return None
    candidates.sort(key=lambda p: -_safe_mtime(p))
    return candidates[0]


def _safe_mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0


def _try_get_target_workers():
    sock_path = _find_harness_socket()
    if not sock_path:
        return None
    try:
        return HarnessSocketClient(sock_path).get_workers()
    except SocketClientError:
        return None


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
        print(f"  {row.skill} ({row.kind}): {passed}/{total}")
    return 0
