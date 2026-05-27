"""Interactive driver: `python3 -m stream_eval.fake <scenarios>`.

Synthesizes one or more scenarios into a directory under
~/.claude/projects/stream-eval-fake-<id>/, prints the path, and
blocks on Ctrl-C so the dashboard can render against it. The
directory is removed at exit.

The dashboard discovers .output files by walking ~/.claude/projects
with Path.rglob, which on Python 3.13+ does NOT follow symlinks --
so we have to write directly into a real directory inside that tree.
A symlink to a tempdir would be invisible to the dashboard.

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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m stream_eval.fake")
    ap.add_argument(
        "scenario", nargs="?",
        help="scenario name, comma-separated list, or 'all' / 'list'",
    )
    ap.add_argument(
        "--base-dir",
        help="write .output files here instead of "
             "~/.claude/projects/stream-eval-fake-<id>/",
    )
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
        # Write directly into ~/.claude/projects/ rather than into a
        # tempdir + symlink. Path.rglob in Python 3.13+ does NOT follow
        # symlinks by default (recurse_symlinks=False), so the
        # dashboard's _output_paths walk would never see fake files
        # under a symlinked dir. Putting the fake dir inside the
        # projects tree directly is the only way to be visible.
        projects = Path.home() / ".claude" / "projects"
        projects.mkdir(parents=True, exist_ok=True)
        base_dir = str(projects / f"stream-eval-fake-{uuid.uuid4().hex[:8]}")

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


if __name__ == "__main__":
    sys.exit(main())
