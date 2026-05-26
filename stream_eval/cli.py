"""Top-level CLI entry point. Subcommands:
- trigger: run the trigger-accuracy harness
- synthesis: run the synthesis-behavior harness
- monitor: serve the live dashboard

Phase B replaces the stub handlers with real implementations.
"""
import argparse
import sys


def _stub(name):
    def handler(_args):
        print(f"stream-eval {name}: not yet implemented", file=sys.stderr)
        return 0
    return handler


def build_parser():
    parser = argparse.ArgumentParser(prog="stream-eval")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_trigger = sub.add_parser("trigger", help="trigger-accuracy harness")
    p_trigger.set_defaults(func=_stub("trigger"))

    p_synth = sub.add_parser("synthesis", help="synthesis-behavior harness")
    p_synth.set_defaults(func=_stub("synthesis"))

    p_monitor = sub.add_parser("monitor", help="live dashboard")
    p_monitor.set_defaults(func=_stub("monitor"))

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
