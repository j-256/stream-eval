"""Tests for stream_eval.monitor.parsing: stderr line + banner regex.

The runner emits these formats; the dashboard parses them. Tests
guard the contract."""
import pytest

from stream_eval.monitor.parsing import (
    parse_finish_banner,
    parse_progress_line,
    parse_startup_banner,
)


def test_parse_progress_line_typical_trigger():
    line = (
        "[3/10] kind=trigger pass=True fixture_id=q0 run=1 elapsed=12.3s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": what scopes does shopper-products getProducts need?"
    )
    parsed = parse_progress_line(line)
    assert parsed["kind"] == "trigger"
    assert parsed["pass_"] is True
    assert parsed["fixture_id"] == "q0"
    assert parsed["run"] == 1
    assert parsed["elapsed"] == 12.3
    assert parsed["retries"] == 0
    assert parsed["timeout_reason"] == "none"
    assert parsed["first_tool"] == "Skill"
    assert parsed["first_skill"] == "dsc-scrape"
    assert parsed["failed_asserts"] == 0
    assert parsed["contaminated"] is False
    assert parsed["query"].startswith("what scopes does")


def test_parse_progress_line_synthesis_with_failed_asserts():
    line = (
        "[5/20] kind=synthesis pass=False fixture_id=mcg-citation-leak "
        "run=2 elapsed=45.7s retries=3 timeout_reason=none first_tool=Bash "
        "first_skill=- failed_asserts=2 contaminated=False"
        ": list the MCG references in the catalog"
    )
    parsed = parse_progress_line(line)
    assert parsed["kind"] == "synthesis"
    assert parsed["pass_"] is False
    assert parsed["failed_asserts"] == 2
    assert parsed["first_skill"] == "-"


def test_parse_progress_line_returns_none_for_non_progress_text():
    assert parse_progress_line("some random log line") is None
    assert parse_progress_line("") is None


def test_parse_startup_banner_typical():
    line = (
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=3 workers=4 "
        "total_fixtures=12 pid=42 ==="
    )
    parsed = parse_startup_banner(line)
    assert parsed["kind"] == "trigger"
    assert parsed["skill"] == "dsc-scrape"
    assert parsed["eval"] == "evals/dsc-scrape/trigger-eval.json"
    assert parsed["runs"] == 3
    assert parsed["workers"] == 4
    assert parsed["total_fixtures"] == 12
    assert parsed["pid"] == 42


def test_parse_startup_banner_legacy_without_pid():
    """Banners written before F.5 don't carry pid=. The parser must
    return pid=None rather than crashing -- otherwise the dashboard
    would fail to render any row whose .output predates F.5."""
    line = (
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=3 workers=4 "
        "total_fixtures=12 ==="
    )
    parsed = parse_startup_banner(line)
    assert parsed is not None
    assert parsed["pid"] is None


def test_parse_startup_banner_returns_none_for_non_banner_text():
    assert parse_startup_banner("[1/10] kind=trigger ...") is None
    assert parse_startup_banner("") is None


def test_parse_finish_banner_completed():
    line = (
        "=== eval finished: kind=trigger skill=dsc-scrape "
        "pid=42 verdict=completed ==="
    )
    parsed = parse_finish_banner(line)
    assert parsed["kind"] == "trigger"
    assert parsed["skill"] == "dsc-scrape"
    assert parsed["pid"] == 42
    assert parsed["verdict"] == "completed"


def test_parse_finish_banner_aborted():
    line = (
        "=== eval finished: kind=synthesis skill=dsc-scenario "
        "pid=99 verdict=aborted ==="
    )
    parsed = parse_finish_banner(line)
    assert parsed["verdict"] == "aborted"


def test_parse_finish_banner_returns_none_for_non_banner_text():
    assert parse_finish_banner("[1/10] kind=trigger ...") is None
    assert parse_finish_banner(
        "=== eval starting: kind=trigger skill=dsc-scrape ..."
    ) is None
    assert parse_finish_banner("") is None
