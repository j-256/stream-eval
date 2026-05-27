"""Tests for stream_eval.monitor.state: walks .output files and builds
DashboardState."""
import pytest

from stream_eval.monitor.state import DashboardState, build_state


@pytest.fixture
def sample_output_file(tmp_path):
    """Write a synthetic .output file with a banner + a few progress lines."""
    p = tmp_path / "session-abc.output"
    p.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=3 workers=4 "
        "total_fixtures=2 ===\n"
        "[1/6] kind=trigger pass=True fixture_id=q0 run=1 elapsed=10s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": query a\n"
        "[2/6] kind=trigger pass=False fixture_id=q1 run=1 elapsed=15s "
        "retries=2 timeout_reason=none first_tool=Skill "
        "first_skill=other failed_asserts=0 contaminated=False"
        ": query b\n"
    )
    return p


def test_build_state_from_one_output_file(sample_output_file):
    state = build_state([sample_output_file])
    assert isinstance(state, DashboardState)
    rows = state.rows
    assert len(rows) == 1
    row = rows[0]
    assert row.skill == "dsc-scrape"
    assert row.kind == "trigger"
    assert row.total_fixtures == 2
    assert row.runs == 3
    pass_count = sum(1 for c in row.cells if c.pass_ is True)
    fail_count = sum(1 for c in row.cells if c.pass_ is False)
    assert pass_count == 1
    assert fail_count == 1


def test_build_state_no_files_yields_empty():
    state = build_state([])
    assert isinstance(state, DashboardState)
    assert state.rows == []


def test_build_state_carries_contaminated_flag(tmp_path):
    """Contamination signal from the runner's progress line must reach
    DashboardCell. The dashboard surfaces it visually; if it falls off
    here, contaminated runs render as if they were clean."""
    p = tmp_path / "session-y.output"
    p.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=1 workers=2 "
        "total_fixtures=2 ===\n"
        "[1/2] kind=trigger pass=True fixture_id=q0 run=1 elapsed=10s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=True"
        ": q\n"
        "[2/2] kind=trigger pass=True fixture_id=q1 run=1 elapsed=10s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    state = build_state([p])
    assert len(state.rows) == 1
    cells = state.rows[0].cells
    contam = [c for c in cells if c.contaminated]
    clean = [c for c in cells if not c.contaminated]
    assert len(contam) == 1
    assert contam[0].fixture_id == "q0"
    assert len(clean) == 1
    assert clean[0].fixture_id == "q1"
