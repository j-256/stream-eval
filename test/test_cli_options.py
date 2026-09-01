import pytest

from stream_eval.fake.__main__ import build_argument_parser as fake_parser
from stream_eval.monitor import build_argument_parser as monitor_parser
from stream_eval.synthesis import build_argument_parser as synthesis_parser
from stream_eval.trigger import build_argument_parser as trigger_parser


def parsed(parser, argv):
    return vars(parser.parse_args(argv))


def harness_arguments(long_options, short_options):
    assert parsed(trigger_parser(), short_options) == parsed(
        trigger_parser(), long_options
    )
    assert parsed(synthesis_parser(), ["-l", *short_options]) == parsed(
        synthesis_parser(), ["--lenient", *long_options]
    )


def test_harness_short_options_match_long_options():
    long_options = [
        "--eval", "eval.json",
        "--out", "out.json",
        "--runs", "2",
        "--workers", "3",
        "--timeout", "4",
        "--cwd", "repo",
        "--agent", "codex",
        "--profile", "restricted",
        "--skill-path", "skill",
        "--also-install", "sibling",
        "--skill-name", "target",
    ]
    short_options = [
        "-e", "eval.json",
        "-o", "out.json",
        "-r", "2",
        "-w", "3",
        "-t", "4",
        "-c", "repo",
        "-a", "codex",
        "-p", "restricted",
        "-s", "skill",
        "-i", "sibling",
        "--skill-name", "target",
    ]
    harness_arguments(long_options, short_options)


def test_monitor_short_options_match_long_options():
    assert parsed(
        monitor_parser(),
        ["serve", "-p", "9000", "-s", "session", "-o"],
    ) == parsed(
        monitor_parser(),
        ["serve", "--port", "9000", "--session", "session", "--open"],
    )
    assert parsed(monitor_parser(), ["summary", "-s", "session"]) == parsed(
        monitor_parser(), ["summary", "--session", "session"]
    )


def test_fake_short_options_match_long_options():
    assert parsed(fake_parser(), ["list", "-b", "state"]) == parsed(
        fake_parser(), ["list", "--base-dir", "state"]
    )


def test_help_documents_short_options_and_collision_exceptions(capsys):
    trigger_help = trigger_parser().format_help()
    assert "-e, --eval EVAL" in trigger_help
    assert "-s, --skill-path SKILL_PATH" in trigger_help
    assert "--skill-name SKILL_NAME" in trigger_help

    with pytest.raises(SystemExit) as exit_info:
        monitor_parser().parse_args(["serve", "--help"])
    assert exit_info.value.code == 0
    monitor_help = capsys.readouterr().out
    assert "-p, --port PORT" in monitor_help
    assert "--host HOST" in monitor_help
