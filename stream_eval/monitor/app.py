"""Flask app for the live dashboard.

Read-only dashboard renders DashboardState. Worker-control routes
(`POST /workers/<action>/<pid>`) talk to a specific harness over its
Unix socket -- per-row routing means a +1 button on the dsc-scrape
row never affects a concurrently-running dsc-triage eval.

Lifecycle routes:
- POST /workers/stop/<pid>: send STOP over the socket; in-flight
  workers finish naturally, no new spawns.
- POST /prune/<pid>: trash the row's .output file. Refuses if the
  pid is still alive (use stop first). Path-traversal guarded.

Public surface:
- create_app(session): Flask application factory; the test client
  uses this.
- run_app(host, port, session, auto_open): production entry point;
  starts the Flask development server.
- print_summary(session): one-shot CLI summary (replaces the old
  no-arg `eval-monitor.py`).
"""
import os
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path

from flask import (
    Flask, render_template, request,
)

from stream_eval.monitor.ps import (
    detect_session,
    find_agent_workers_for,
    find_output_files,
)
from stream_eval.monitor.socket_client import (
    HarnessSocketClient, SocketClientError,
)
from stream_eval.monitor.state import build_state
from stream_eval.paths import output_dirs, state_dir


def create_app(session=None):
    app = Flask(__name__)
    app.jinja_env.filters["strftime"] = _strftime_local
    app.jinja_env.filters["start_label"] = _format_start_label

    def _resolve_session():
        return session or detect_session()

    def _gather_state():
        state = build_state(find_output_files())
        for row in state.rows:
            row.target_workers, row.dispatcher_state = _row_socket_snapshot(row)
            row.workers_for_row = _agent_workers_for_row(row)
            row.recent = _recent_for_row(row)
            row.in_flight_count = len(row.workers_for_row)
            row.in_flight_retries = sum(
                w.get("retries", 0) for w in row.workers_for_row
            )
            # Cumulative retries across the whole run: completed runs'
            # retries (persisted on each cell from the progress line)
            # plus the live workers' in-progress retries. The live tally
            # alone drops back to 0 the instant a retrying worker exits
            # -- and is always 0 for trigger evals, whose transcripts are
            # unlinked tempfiles the worker-scanner can't read. Summing
            # the cells makes the number monotonic and meaningful: a run
            # that hit 3 retries keeps contributing 3 after it finishes.
            row.total_retries = (
                sum(c.retries for c in row.cells) + row.in_flight_retries
            )
        return {
            "state": state,
            "session": _resolve_session(),
            "poll_active_ms": _poll_default("STREAM_EVAL_POLL_ACTIVE_MS", 5000),
            "poll_idle_ms": _poll_default("STREAM_EVAL_POLL_IDLE_MS", 30000),
            # `now` for stop-dialog runtime estimates -- the row carries
            # ctime, the template subtracts to render "running for Xm"
            # without needing JS-side clock math.
            "now": time.time(),
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

    @app.route("/workers/stop/<int:pid>", methods=["POST"],
               endpoint="workers_stop")
    def workers_stop(pid):
        return _action_response_for_pid(pid, lambda c: c.stop())

    @app.route("/prune/<int:pid>", methods=["POST"], endpoint="prune_row")
    def prune_row(pid):
        """Trash the .output file for a non-active row.

        Refuses if the pid is still alive: a running harness's tee
        is still writing to the file, and either the trash would
        silently re-appear or the tee would break. Use STOP first.

        Macs get `mv ~/.Trash/` (recoverable from Finder); other
        platforms fall back to os.unlink. Path-traversal guarded:
        only paths under the canonical .output dir are removable.
        """
        candidates = []
        for base in output_dirs():
            candidates.append(base / f"{pid}.output")
            candidates.append(base / "stream-eval" / f"{pid}.output")
        target = next((path for path in candidates if path.exists()), None)
        if target is None:
            target = state_dir() / f"{pid}.output"
        log_dir = target.parent
        # Path-traversal guard: resolve and verify the target stays
        # inside log_dir. Pid is already constrained by the int route
        # converter, but the parent dir could theoretically be
        # symlinked elsewhere -- we want to fail closed.
        try:
            resolved = target.resolve(strict=False)
            log_dir_resolved = log_dir.resolve(strict=False)
            if log_dir_resolved not in resolved.parents:
                ctx = _gather_state()
                return render_template(
                    "partials/_dashboard_main.html", **ctx,
                ), 400
        except (OSError, ValueError):
            ctx = _gather_state()
            return render_template(
                "partials/_dashboard_main.html", **ctx,
            ), 400

        # Active-pid guard. _is_pid_alive is the same probe state.py
        # uses to decide row status; consistent here.
        if _is_pid_alive(pid):
            ctx = _gather_state()
            return render_template(
                "partials/_dashboard_main.html", **ctx,
            ), 409  # Conflict -- stop the run first.

        if target.exists():
            _trash_file(target)

        ctx = _gather_state()
        return render_template("partials/_dashboard_main.html", **ctx)

    return app


def _is_pid_alive(pid):
    """Cheap pid-liveness probe. Returns True if the pid is alive,
    False if it's gone or unreachable."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _trash_file(path):
    """Move `path` to the recoverable trash on macOS, fall back to
    unlink elsewhere. Best-effort: failures are silently swallowed
    because the dashboard's prune route doesn't have a great place to
    surface 'we couldn't trash this' errors -- the row staying
    visible after the click is itself the error signal."""
    if shutil.which("trash"):
        # macOS `trash` CLI handles name conflicts in ~/.Trash/ via
        # numeric suffixes, no extra logic needed.
        try:
            subprocess.run(
                ["trash", str(path)],
                check=False,
                capture_output=True,
                timeout=5,
            )
            if not path.exists():
                return
        except (subprocess.SubprocessError, OSError):
            pass
    if os.uname().sysname == "Darwin":
        # Mac without `trash` CLI: mv to ~/.Trash directly. Adds a
        # numeric suffix on conflict to match Finder's behavior.
        trash_dir = Path.home() / ".Trash"
        if trash_dir.is_dir():
            target = trash_dir / path.name
            n = 1
            while target.exists():
                target = trash_dir / f"{path.stem} {n}{path.suffix}"
                n += 1
            try:
                shutil.move(str(path), str(target))
                return
            except OSError:
                pass
    # Last resort: permanent unlink.
    try:
        path.unlink()
    except OSError:
        pass


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


def _agent_workers_for_row(row):
    """Return [{pid, started_at_human, cmdline}, ...] for the live
    agent CLI children of this row's harness pid. Empty list for
    completed/aborted/unknown rows or when the harness has no children
    at this poll instant."""
    if row.status != "active" or row.harness_pid is None:
        return []
    out = []
    for w in find_agent_workers_for(row.harness_pid):
        w["started_at_human"] = _humanize(time.time() - w["started_at"])
        out.append(w)
    return out


def _recent_for_row(row):
    """Last 5 cells for this row, rendered as recent-completion records.

    DashboardCell carries (fixture_id, run, pass_, contaminated,
    retries). The runner's progress line carries still more
    (elapsed/first_tool/first_skill/asserts/query) that state.py
    discards after computing pass/fail; plumbing those through
    DashboardCell would let this table reach the original
    eval-monitor's depth."""
    out = []
    for cell in reversed(row.cells[-5:]):
        out.append({
            "fixture_id": cell.fixture_id,
            "run": cell.run,
            "pass_": cell.pass_ if cell.pass_ is not None else False,
            "contaminated": cell.contaminated,
            "retries": cell.retries,
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


def _strftime_local(epoch_seconds, fmt="%H:%M"):
    """Format a unix timestamp in the dashboard's local timezone. Used
    by the row header's tooltip to render the full started_at date/time.
    Returns an empty string for None so the template can `{% if %}`
    cleanly."""
    if epoch_seconds is None:
        return ""
    return time.strftime(fmt, time.localtime(epoch_seconds))


def _format_start_label(epoch_seconds, now=None):
    """Format started_at for the row header's inline label.

    A bare HH:MM repeats every 24h, so a dashboard listing several days
    of evals can't disambiguate "started 09:14 today" from "09:14 three
    days ago" -- and relying on the hover tooltip for the date is poor
    UX (browsers delay it ~1s). So we put the date inline whenever the
    run did NOT start today: "Jun 08 09:14". Same-day runs (the common
    case) stay compact at "09:14".

    `now` defaults to time.time(); injectable for deterministic tests.
    Returns "" for None.
    """
    if epoch_seconds is None:
        return ""
    if now is None:
        now = time.time()
    start_lt = time.localtime(epoch_seconds)
    now_lt = time.localtime(now)
    same_day = (
        (start_lt.tm_year, start_lt.tm_yday)
        == (now_lt.tm_year, now_lt.tm_yday)
    )
    if same_day:
        return time.strftime("%H:%M", start_lt)
    return time.strftime("%b %d %H:%M", start_lt)


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
