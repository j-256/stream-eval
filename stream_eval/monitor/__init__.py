"""Live dashboard for stream-eval.

Public surface:
- main(argv): CLI entry point; argv-list-aware so stream_eval.cli can
  forward sys.argv[1:].

Internal modules (NOT public API):
- parsing.py: stderr line + banner regex.
- ps.py: psutil-based process discovery + session detection.
- state.py: .output file walking + DashboardState.
- app.py: Flask app + worker-control routes.
- socket_client.py: HarnessSocketClient adapter for the harness
  Unix-socket protocol.
"""
import argparse


def main(argv=None):
    """CLI entry point for `stream-eval monitor`.

    Subcommands:
    - serve: start the Flask app at http://<host>:<port>/.
    - summary: one-shot CLI summary (no server).

    If no subcommand is given, defaults to 'summary'.
    """
    parser = argparse.ArgumentParser(prog="stream-eval monitor")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="start the Flask app")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--session", default=None,
                         help="label the dashboard with an agent session id")
    p_serve.add_argument("--open", action="store_true",
                         help="open the dashboard in the default browser")

    p_summary = sub.add_parser("summary", help="one-shot CLI summary")
    p_summary.add_argument("--session", default=None)

    args = parser.parse_args(argv)
    cmd = args.cmd or "summary"

    if cmd == "serve":
        from stream_eval.monitor.app import run_app
        return run_app(
            host=args.host, port=args.port,
            session=args.session, auto_open=args.open,
        )
    if cmd == "summary":
        from stream_eval.monitor.app import print_summary
        return print_summary(session=args.session)
    parser.print_help()
    return 2
