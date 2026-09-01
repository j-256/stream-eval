"""Interactive driver: `python3 -m stream_eval.fake <scenarios>`

Synthesizes one or more scenarios in the stream-eval state directory,
prints the path, and blocks on Ctrl-C so the dashboard can render against
it. The directory is removed at exit.

Usage:
    python3 -m stream_eval.fake concurrent
    python3 -m stream_eval.fake concurrent,over-cap,legacy
    python3 -m stream_eval.fake all
    python3 -m stream_eval.fake list

Real operation has every state coexisting; pass several scenarios
(comma-separated) or `all` to render them simultaneously.
"""
import argparse
import signal
import sys
import uuid
from pathlib import Path

from stream_eval.fake import SCENARIOS, make_fake_state
from stream_eval.paths import state_dir


def build_argument_parser():
    ap = argparse.ArgumentParser(prog="python3 -m stream_eval.fake")
    ap.add_argument(
        "scenario", nargs="?",
        help="scenario name, comma-separated list, or 'all' / 'list'",
    )
    ap.add_argument(
        "-b", "--base-dir",
        help="write .output files here instead of "
             "the stream-eval state directory",
    )
    return ap


def main(argv=None):
    ap = build_argument_parser()
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

    base_dir = args.base_dir
    if base_dir is None:
        state_root = state_dir()
        state_root.mkdir(parents=True, exist_ok=True)
        _cleanup_orphaned_fake_dirs(state_root)
        base_dir = str(
            state_root / f"stream-eval-fake-{uuid.uuid4().hex[:8]}"
        )

    state = make_fake_state(names, base_dir=base_dir)
    print(f"scenarios: {', '.join(names)}")
    print(f"base_dir: {state.base_dir}")
    print(f"fake harness pids: {sorted(state.fake_pids)}")
    print(f"fake sockets: {[s.socket_path for s in state.sockets]}")
    print()
    print("Start the dashboard in another terminal:")
    print("  stream-eval monitor serve --port 8765 --open")
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
        state.close()
        print("torn down.")

    return 0


def _cleanup_orphaned_fake_dirs(state_root):
    """Remove leftover stream-eval-fake-<id>/ directories from prior
    sessions that didn't tear down cleanly (kill -9, OS reboot,
    etc.). Skips dirs that contain fake harnesses with active socket
    files in /tmp -- those might still be in use by a running session.
    """
    import shutil
    for child in state_root.glob("stream-eval-fake-*"):
        if not child.is_dir():
            continue
        # Read each *.output file's startup banner pid; if any of
        # those pids has a live socket at /tmp/stream-eval-<pid>.sock,
        # something's still using this dir and we should not touch it.
        live = False
        for output in child.glob("*.output"):
            try:
                first_line = output.read_text().splitlines()[0]
            except (OSError, IndexError):
                continue
            import re
            m = re.search(r"pid=(\d+)", first_line)
            if m and Path(f"/tmp/stream-eval-{m.group(1)}.sock").exists():
                live = True
                break
        if live:
            continue
        try:
            shutil.rmtree(child)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
