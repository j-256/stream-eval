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
        "total_fixtures=2 pid=4242 ===\n"
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
    assert row.harness_pid == 4242
    assert row.mtime > 0
    pass_count = sum(1 for c in row.cells if c.pass_ is True)
    fail_count = sum(1 for c in row.cells if c.pass_ is False)
    assert pass_count == 1
    assert fail_count == 1


def test_build_state_separates_concurrent_evals_of_same_skill_kind(tmp_path):
    """Two evals of the same (skill, kind) running concurrently must
    produce two distinct rows -- otherwise their cells interleave and
    worker-control buttons can't route to the right harness pid."""
    p1 = tmp_path / "session-A.output"
    p1.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=1 workers=2 "
        "total_fixtures=1 pid=11111 ===\n"
        "[1/1] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    p2 = tmp_path / "session-B.output"
    p2.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=1 workers=2 "
        "total_fixtures=1 pid=22222 ===\n"
        "[1/1] kind=trigger pass=False fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    state = build_state([p1, p2])
    assert len(state.rows) == 2
    pids = {r.harness_pid for r in state.rows}
    assert pids == {11111, 22222}


def test_build_state_legacy_banner_without_pid(tmp_path):
    """Legacy .output files written before F.5 don't carry pid=. Their
    rows must still appear; harness_pid is None and the dashboard will
    render them with status='unknown' and no controls."""
    p = tmp_path / "legacy.output"
    p.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=1 workers=1 "
        "total_fixtures=1 ===\n"
        "[1/1] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    state = build_state([p])
    assert len(state.rows) == 1
    assert state.rows[0].harness_pid is None
    assert state.rows[0].status == "unknown"


def test_build_state_status_active_when_pid_alive(tmp_path):
    """Row without a finish banner whose harness pid is alive is
    'active' -- the eval is mid-run."""
    p = tmp_path / "active.output"
    p.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=1 workers=1 "
        "total_fixtures=1 pid=55555 ===\n"
        "[1/1] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    state = build_state([p], is_pid_alive=lambda pid: pid == 55555)
    assert state.rows[0].status == "active"


def test_build_state_status_aborted_when_pid_dead_no_finish(tmp_path):
    """No finish banner + dead pid = aborted. The runner crashed
    before it could stamp the verdict, or the user Ctrl-Cd it."""
    p = tmp_path / "aborted.output"
    p.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=1 workers=1 "
        "total_fixtures=1 pid=55555 ===\n"
        "[1/1] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
    )
    state = build_state([p], is_pid_alive=lambda pid: False)
    assert state.rows[0].status == "aborted"


def test_build_state_status_completed_with_finish_banner(tmp_path):
    """Finish banner with verdict=completed always wins, even if the
    pid is somehow still alive (shouldn't happen, but the banner is
    the authoritative signal)."""
    p = tmp_path / "completed.output"
    p.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=1 workers=1 "
        "total_fixtures=1 pid=55555 ===\n"
        "[1/1] kind=trigger pass=True fixture_id=q0 run=1 elapsed=5s "
        "retries=0 timeout_reason=none first_tool=Skill "
        "first_skill=dsc-scrape failed_asserts=0 contaminated=False"
        ": q\n"
        "=== eval finished: kind=trigger skill=dsc-scrape "
        "pid=55555 verdict=completed ===\n"
    )
    state = build_state([p], is_pid_alive=lambda pid: True)
    assert state.rows[0].status == "completed"


def test_build_state_status_aborted_with_finish_banner(tmp_path):
    """Finish banner with verdict=aborted (timeout/throttle bail) is
    the authoritative signal."""
    p = tmp_path / "aborted-finish.output"
    p.write_text(
        "=== eval starting: kind=trigger skill=dsc-scrape "
        "eval=evals/dsc-scrape/trigger-eval.json runs=1 workers=1 "
        "total_fixtures=1 pid=55555 ===\n"
        "=== eval finished: kind=trigger skill=dsc-scrape "
        "pid=55555 verdict=aborted ===\n"
    )
    state = build_state([p], is_pid_alive=lambda pid: True)
    assert state.rows[0].status == "aborted"


def _write_completed(tmp_path, name, pid, mtime, skill="dsc-scrape"):
    """Helper: emit a finished .output file with a controlled mtime."""
    p = tmp_path / f"{name}.output"
    p.write_text(
        f"=== eval starting: kind=trigger skill={skill} "
        f"eval=evals/{skill}/trigger-eval.json runs=1 workers=1 "
        f"total_fixtures=1 pid={pid} ===\n"
        f"=== eval finished: kind=trigger skill={skill} "
        f"pid={pid} verdict=completed ===\n"
    )
    import os as _os
    _os.utime(p, (mtime, mtime))
    return p


def test_build_state_per_skill_cap_caps_completed_rows(tmp_path):
    """Six completed rows for one (skill, kind), cap=3 -> only the
    youngest 3 survive. Older completions are hidden."""
    paths = [
        _write_completed(tmp_path, f"r{i}", pid=1000 + i, mtime=1000.0 + i)
        for i in range(6)
    ]
    state = build_state(
        paths,
        is_pid_alive=lambda pid: False,
        per_skill_cap=3,
    )
    assert len(state.rows) == 3
    pids = {r.harness_pid for r in state.rows}
    # Youngest three -- the high mtimes -- are 3, 4, 5.
    assert pids == {1003, 1004, 1005}


def test_build_state_per_skill_cap_keeps_all_active(tmp_path):
    """Six active rows for one (skill, kind), cap=3. Active rows must
    NOT be hidden: a user with 6 concurrent evals running needs to see
    all of them, otherwise the per-row controls become unreachable."""
    paths = [
        _write_completed(tmp_path, f"r{i}", pid=2000 + i, mtime=1000.0 + i)
        for i in range(6)
    ]
    # Pretend every harness pid is alive.
    state = build_state(
        paths,
        is_pid_alive=lambda pid: True,
        per_skill_cap=3,
    )
    # No finish banners would be present in real "active" runs, but the
    # helper writes one. The finish banner takes precedence in the
    # state machine, so for this test we strip it out and re-write.
    for p in paths:
        p.write_text(p.read_text().replace(
            "=== eval finished:", "# (suppressed for test) eval finished:"
        ))
    state = build_state(
        paths,
        is_pid_alive=lambda pid: True,
        per_skill_cap=3,
    )
    assert len(state.rows) == 6
    assert all(r.status == "active" for r in state.rows)


def test_build_state_per_skill_cap_disabled_with_zero(tmp_path):
    """per_skill_cap=0 disables the cap entirely (one-shot CLI summary
    with everything)."""
    paths = [
        _write_completed(tmp_path, f"r{i}", pid=3000 + i, mtime=1000.0 + i)
        for i in range(10)
    ]
    state = build_state(
        paths,
        is_pid_alive=lambda pid: False,
        per_skill_cap=0,
    )
    assert len(state.rows) == 10


def test_build_state_orders_active_rows_first_then_recent(tmp_path):
    """Active runs first (the operator's focus), then recency-ordered
    inside each status bucket. A just-finished eval lands at the top
    of the completed bucket -- right under any active rows -- rather
    than getting alphabetically buried mid-list."""
    import time
    now = time.time()

    # An old completed run, an alphabetically-earlier active run, and
    # a freshly-completed run.
    p_old = tmp_path / "old-completed.output"
    p_old.write_text(
        "=== eval starting: kind=trigger skill=zzz-skill "
        "eval=evals/zzz/trigger-eval.json runs=1 workers=1 "
        "total_fixtures=1 pid=70001 ===\n"
        "=== eval finished: kind=trigger skill=zzz-skill "
        "pid=70001 verdict=completed ===\n"
    )
    import os as _os
    _os.utime(p_old, (now - 3600, now - 3600))

    p_active = tmp_path / "active.output"
    p_active.write_text(
        "=== eval starting: kind=trigger skill=aaa-skill "
        "eval=evals/aaa/trigger-eval.json runs=1 workers=1 "
        "total_fixtures=1 pid=70002 ===\n"
    )
    _os.utime(p_active, (now - 60, now - 60))

    p_fresh = tmp_path / "fresh-completed.output"
    p_fresh.write_text(
        "=== eval starting: kind=trigger skill=mmm-skill "
        "eval=evals/mmm/trigger-eval.json runs=1 workers=1 "
        "total_fixtures=1 pid=70003 ===\n"
        "=== eval finished: kind=trigger skill=mmm-skill "
        "pid=70003 verdict=completed ===\n"
    )
    _os.utime(p_fresh, (now, now))

    state = build_state(
        [p_old, p_active, p_fresh],
        is_pid_alive=lambda pid: pid == 70002,
    )
    assert [(r.skill, r.status) for r in state.rows] == [
        ("aaa-skill", "active"),
        ("mmm-skill", "completed"),
        ("zzz-skill", "completed"),
    ]


def test_build_state_orphan_finish_banner_does_not_crash(tmp_path):
    """A finish banner whose pid never had a startup banner (impossible
    in practice, but a malformed .output file shouldn't crash the
    dashboard) is silently dropped: no row is emitted for it."""
    p = tmp_path / "orphan.output"
    p.write_text(
        "=== eval finished: kind=trigger skill=ghost "
        "pid=77777 verdict=completed ===\n"
    )
    state = build_state([p], is_pid_alive=lambda pid: False)
    assert state.rows == []


def test_build_state_per_skill_cap_underflow_when_active_exceeds_cap(tmp_path):
    """Cap=2, active_count=4. The cap math (max(cap - active, 0)) must
    not return a negative slot count; all 4 active rows survive."""
    paths = []
    for i in range(4):
        p = tmp_path / f"a{i}.output"
        p.write_text(
            f"=== eval starting: kind=trigger skill=dsc-scrape "
            f"eval=evals/dsc-scrape/trigger-eval.json runs=1 workers=1 "
            f"total_fixtures=1 pid={6000 + i} ===\n"
        )
        paths.append(p)
    state = build_state(
        paths,
        is_pid_alive=lambda pid: True,
        per_skill_cap=2,
    )
    assert len(state.rows) == 4
    assert all(r.status == "active" for r in state.rows)


def test_build_state_per_skill_cap_separate_skills_get_separate_caps(tmp_path):
    """Cap applies per (skill, kind) independently. Cap=2 with 3 of
    skill A and 3 of skill B -> 4 rows total, 2 per skill."""
    a_paths = [
        _write_completed(tmp_path, f"a{i}", pid=4000 + i, mtime=1000.0 + i,
                         skill="dsc-scrape")
        for i in range(3)
    ]
    b_paths = [
        _write_completed(tmp_path, f"b{i}", pid=5000 + i, mtime=1000.0 + i,
                         skill="dsc-triage")
        for i in range(3)
    ]
    state = build_state(
        a_paths + b_paths,
        is_pid_alive=lambda pid: False,
        per_skill_cap=2,
    )
    assert len(state.rows) == 4
    by_skill = {}
    for r in state.rows:
        by_skill.setdefault(r.skill, []).append(r)
    assert len(by_skill["dsc-scrape"]) == 2
    assert len(by_skill["dsc-triage"]) == 2


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
        "total_fixtures=2 pid=33333 ===\n"
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
