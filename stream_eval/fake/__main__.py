"""Interactive driver: `python3 -m stream_eval.fake <scenarios>`.

Synthesizes one or more scenarios into a temp dir, prints the path,
and blocks on Ctrl-C so the dashboard can render against it. The
dashboard discovers fake .output files via ps._output_paths ->
~/.claude/projects.

To make the fake visible to a real running dashboard, this driver
symlinks the temp dir under ~/.claude/projects/stream-eval-fake/ for
the duration of the session. The symlink is removed at exit.

Usage:
    python3 -m stream_eval.fake concurrent
    python3 -m stream_eval.fake concurrent,over-cap,legacy
    python3 -m stream_eval.fake all
    python3 -m stream_eval.fake list

Real operation has every state coexisting; pass several scenarios
(comma-separated) or `all` to render them simultaneously.
"""
import argparse
import os
import signal
import sys
from pathlib import Path

from stream_eval.fake import SCENARIOS, make_fake_state


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m stream_eval.fake")
    ap.add_argument(
        "scenario", nargs="?",
        help="scenario name, comma-separated list, or 'all' / 'list'",
    )
    ap.add_argument("--base-dir",
                    help="write .output files here instead of a tempdir")
    ap.add_argument("--no-symlink", action="store_true",
                    help="don't symlink under ~/.claude/projects/")
    args = ap.parse_args(argv)

    if not args.scenario or args.scenario == "list":
        print("Scenarios:")
        for name in sorted(SCENARIOS):
            print(f"  {name}")
        print("\nPass one, several (comma-separated), or 'all'.")
        return 0

    if args.scenario == "all":
        # Drop full-spread when expanding 'all' since it's a meta-
        # scenario that composes the others -- including it would
        # duplicate every state.
        names = sorted(n for n in SCENARIOS if n != "full-spread")
    else:
        names = [s.strip() for s in args.scenario.split(",") if s.strip()]

    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"unknown scenarios: {unknown}", file=sys.stderr)
        print(f"choices: {sorted(SCENARIOS)}", file=sys.stderr)
        return 2

    state = make_fake_state(names, base_dir=args.base_dir)
    print(f"scenarios: {', '.join(names)}")
    print(f"base_dir: {state.base_dir}")
    print(f"fake harness pids: {sorted(state.fake_pids)}")
    print(f"fake sockets: {[s.socket_path for s in state.sockets]}")

    symlink_path = None
    if not args.no_symlink:
        projects = Path.home() / ".claude" / "projects"
        if projects.is_dir():
            symlink_path = projects / "stream-eval-fake"
            try:
                if symlink_path.is_symlink() or symlink_path.exists():
                    symlink_path.unlink()
                os.symlink(state.base_dir, symlink_path)
                print(f"symlinked: {symlink_path} -> {state.base_dir}")
            except OSError as exc:
                print(f"could not symlink ({exc}); dashboard won't see "
                      f"this scenario unless STREAM_EVAL_OUTPUT_LIMIT or "
                      f"its file walk picks up {state.base_dir} directly",
                      file=sys.stderr)
                symlink_path = None

    print()
    print("Start the dashboard in another terminal:")
    print("  stream-eval monitor serve --port 8765")
    print()
    print("Press Ctrl+C to tear down.")

    stop = False

    def _on_sig(_sig, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    try:
        signal.pause()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if symlink_path is not None:
            try:
                symlink_path.unlink()
            except OSError:
                pass
        state.close()
        print("torn down.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
