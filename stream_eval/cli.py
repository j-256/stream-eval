"""Top-level CLI entry point for stream-eval. Subcommands dispatch to
the per-kind harness modules. Each module owns its own argparse and
returns an exit code from its `main()`.
"""
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _print_help()
        return 2

    cmd = argv[0]
    rest = argv[1:]

    if cmd in ("-h", "--help"):
        _print_help()
        return 0

    if cmd == "trigger":
        from stream_eval.trigger import main as trigger_main
        return trigger_main(rest)
    if cmd == "synthesis":
        from stream_eval.synthesis import main as synthesis_main
        return synthesis_main(rest)
    if cmd == "monitor":
        from stream_eval.monitor import main as monitor_main
        return monitor_main(rest)

    print(f"stream-eval: unknown subcommand: {cmd!r}", file=sys.stderr)
    _print_help()
    return 2


def _print_help():
    print(
        "usage: stream-eval <subcommand> [options]\n"
        "\n"
        "Subcommands:\n"
        "  trigger    run the trigger-accuracy harness\n"
        "  synthesis  run the synthesis-behavior harness\n"
        "  monitor    serve the live dashboard\n"
        "\n"
        "Run `stream-eval <subcommand> --help` for subcommand-specific options.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
