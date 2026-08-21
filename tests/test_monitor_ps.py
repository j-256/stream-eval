"""Tests for stream_eval.monitor.ps: process discovery + session
detection. We mock psutil to avoid relying on the host's actual
process tree."""
from unittest import mock

import pytest


def _fake_proc(pid, ppid, name, cmdline, create_time):
    p = mock.MagicMock()
    p.pid = pid
    p.ppid = mock.MagicMock(return_value=ppid)
    p.name = mock.MagicMock(return_value=name)
    p.cmdline = mock.MagicMock(return_value=cmdline)
    p.create_time = mock.MagicMock(return_value=create_time)
    p.is_running = mock.MagicMock(return_value=True)
    return p


def test_find_eval_workers_returns_running_trigger_and_synthesis_processes():
    from stream_eval.monitor.ps import find_eval_workers

    procs = [
        _fake_proc(101, 100, "python3",
                   ["python3", "-m", "stream_eval.cli", "trigger",
                    "--skill-path", "/skills/dsc-scrape", "--eval", "x.json"],
                   1.0),
        _fake_proc(102, 100, "python3",
                   ["python3", "-m", "stream_eval.cli", "synthesis",
                    "--eval", "y.json"],
                   2.0),
        _fake_proc(103, 100, "vim", ["vim", "foo.py"], 3.0),
    ]
    with mock.patch("stream_eval.monitor.ps.psutil.process_iter",
                    return_value=procs):
        workers = list(find_eval_workers())
    kinds = sorted(w["kind"] for w in workers)
    assert kinds == ["synthesis", "trigger"]


def test_find_eval_workers_skips_non_running_processes():
    from stream_eval.monitor.ps import find_eval_workers

    dead = _fake_proc(101, 100, "python3",
                      ["python3", "-m", "stream_eval.cli", "trigger"],
                      1.0)
    dead.is_running = mock.MagicMock(return_value=False)

    with mock.patch("stream_eval.monitor.ps.psutil.process_iter",
                    return_value=[dead]):
        workers = list(find_eval_workers())
    assert workers == []


def test_find_eval_workers_recognizes_console_script_form():
    """`stream-eval trigger ...` (console script) is a different
    cmdline shape from `python -m stream_eval.cli trigger ...`. Both
    must be detected."""
    from stream_eval.monitor.ps import find_eval_workers

    proc = _fake_proc(
        201, 100, "python3",
        ["/repo/.venv/bin/python3", "/repo/.venv/bin/stream-eval",
         "trigger", "--skill-path", "/skills/dsc", "--eval", "z.json"],
        4.0,
    )
    with mock.patch("stream_eval.monitor.ps.psutil.process_iter",
                    return_value=[proc]):
        workers = list(find_eval_workers())
    assert len(workers) == 1
    assert workers[0]["kind"] == "trigger"


def test_find_claude_workers_for_returns_claude_children():
    """Children of the harness pid that are running `claude -p` should
    surface; non-claude children (e.g. a git subprocess) should not."""
    from stream_eval.monitor.ps import find_claude_workers_for

    parent = _fake_proc(
        100, 1, "python3",
        ["python3", "-m", "stream_eval.cli", "trigger"],
        1.0,
    )
    child_claude = _fake_proc(
        201, 100, "claude",
        ["claude", "-p", "--output-format", "stream-json"],
        2.0,
    )
    child_git = _fake_proc(
        202, 100, "git",
        ["git", "status", "--porcelain"],
        2.5,
    )
    parent.children = mock.MagicMock(return_value=[child_claude, child_git])

    with mock.patch("stream_eval.monitor.ps.psutil.Process",
                    return_value=parent):
        workers = list(find_claude_workers_for(100))
    assert len(workers) == 1
    assert workers[0]["pid"] == 201
    assert workers[0]["agent"] == "claude"


def test_find_agent_workers_for_returns_codex_children():
    from stream_eval.monitor.ps import find_agent_workers_for

    parent = _fake_proc(
        100, 1, "python3",
        ["python3", "-m", "stream_eval.cli", "trigger"],
        1.0,
    )
    child_codex = _fake_proc(
        203, 100, "codex",
        ["/usr/local/bin/codex", "exec", "--json", "test query"],
        2.0,
    )
    child_git = _fake_proc(
        204, 100, "git",
        ["git", "status", "--porcelain"],
        2.5,
    )
    parent.children = mock.MagicMock(return_value=[child_codex, child_git])

    with mock.patch("stream_eval.monitor.ps.psutil.Process",
                    return_value=parent):
        workers = list(find_agent_workers_for(100))
    assert len(workers) == 1
    assert workers[0]["pid"] == 203
    assert workers[0]["agent"] == "codex"


def test_find_agent_workers_for_returns_opencode_children():
    from stream_eval.monitor.ps import find_agent_workers_for

    parent = _fake_proc(
        100, 1, "python3",
        ["python3", "-m", "stream_eval.cli", "trigger"],
        1.0,
    )
    child_opencode = _fake_proc(
        205, 100, "opencode",
        ["/usr/local/bin/opencode", "run", "--format", "json"],
        2.0,
    )
    child_git = _fake_proc(
        206, 100, "git",
        ["git", "status", "--porcelain"],
        2.5,
    )
    parent.children = mock.MagicMock(
        return_value=[child_opencode, child_git]
    )

    with mock.patch(
        "stream_eval.monitor.ps.psutil.Process",
        return_value=parent,
    ):
        workers = list(find_agent_workers_for(100))
    assert len(workers) == 1
    assert workers[0]["pid"] == 205
    assert workers[0]["agent"] == "opencode"


def test_find_claude_workers_for_rejects_git_subprocess_with_claude_in_path():
    """The runner spawns git for worktree management. A git invocation
    whose --git-dir path happens to contain 'claude' (e.g.
    /Users/me/claude-adapter-fixture/.git) must NOT be reported as a
    claude worker -- only argv[0]'s basename being exactly 'claude'
    counts."""
    from stream_eval.monitor.ps import find_claude_workers_for

    parent = _fake_proc(
        100, 1, "python3",
        ["python3", "-m", "stream_eval.cli", "trigger"],
        1.0,
    )
    fake_git = _fake_proc(
        300, 100, "git",
        ["git",
         "--git-dir=/Users/me/claude-adapter-fixture/.git",
         "status", "--porcelain"],
        2.0,
    )
    parent.children = mock.MagicMock(return_value=[fake_git])

    with mock.patch("stream_eval.monitor.ps.psutil.Process",
                    return_value=parent):
        workers = list(find_claude_workers_for(100))
    assert workers == []


def test_find_claude_workers_for_accepts_absolute_path_to_claude():
    """argv[0] being /usr/local/bin/claude is the common production
    shape -- the basename match must accept it."""
    from stream_eval.monitor.ps import find_claude_workers_for

    parent = _fake_proc(100, 1, "python3", ["python3", "-m", "x"], 1.0)
    abs_claude = _fake_proc(
        301, 100, "claude",
        ["/usr/local/bin/claude", "-p", "--output-format", "stream-json"],
        2.0,
    )
    parent.children = mock.MagicMock(return_value=[abs_claude])
    with mock.patch("stream_eval.monitor.ps.psutil.Process",
                    return_value=parent):
        workers = list(find_claude_workers_for(100))
    assert len(workers) == 1
    assert workers[0]["pid"] == 301


def test_find_claude_workers_for_includes_retry_fields():
    """Each yielded worker dict carries retries/latest_attempt/
    max_retries_field/last_error so the dashboard can show the
    'X in flight, Y retries' header and the legacy attempt color."""
    from stream_eval.monitor.ps import find_claude_workers_for

    parent = _fake_proc(100, 1, "python3", ["python3", "-m", "x"], 1.0)
    claude = _fake_proc(
        201, 100, "claude",
        ["claude", "-p"],
        2.0,
    )
    parent.children = mock.MagicMock(return_value=[claude])
    # Empty open_files() -> falls through to lsof, which we mock to
    # return no transcript path.
    claude.open_files = mock.MagicMock(return_value=[])

    with mock.patch("stream_eval.monitor.ps.psutil.Process",
                    return_value=parent), \
         mock.patch("stream_eval.monitor.ps.subprocess.run") as mock_run:
        mock_run.return_value = mock.MagicMock(returncode=0, stdout="")
        workers = list(find_claude_workers_for(100))
    assert len(workers) == 1
    w = workers[0]
    assert w["retries"] == 0
    assert w["latest_attempt"] == 0
    assert w["fixture_id"] is None
    assert w["transcript_path"] is None


def test_find_claude_workers_for_returns_empty_when_parent_missing():
    """A dead/never-existed harness pid yields nothing rather than
    raising. The dashboard polls into this every refresh and must
    tolerate the harness exiting between renders."""
    import psutil

    from stream_eval.monitor.ps import find_claude_workers_for

    with mock.patch("stream_eval.monitor.ps.psutil.Process",
                    side_effect=psutil.NoSuchProcess(pid=999)):
        workers = list(find_claude_workers_for(999))
    assert workers == []


def test_transcript_stats_counts_api_retries(tmp_path):
    """The retry counter underpins the dashboard's 'Y retries in
    flight' header. Each api_retry event in the transcript JSONL
    increments total_retries; the most-recent event sets
    latest_attempt and last_error."""
    from stream_eval.monitor.ps import _transcript_stats

    tx = tmp_path / "q0-1.jsonl"
    tx.write_text(
        '{"type":"system","subtype":"api_retry","attempt":1,"max_retries":10,"error":"rate_limit"}\n'
        '{"type":"assistant","message":{"content":"working"}}\n'
        '{"type":"system","subtype":"api_retry","attempt":2,"max_retries":10,"error":"server_error"}\n'
        '{"type":"system","subtype":"api_retry","attempt":3,"max_retries":10,"error":"rate_limit"}\n'
    )
    stats = _transcript_stats(str(tx))
    assert stats["total_retries"] == 3
    assert stats["latest_attempt"] == 3
    assert stats["max_retries_field"] == 10
    assert stats["last_error"] == "rate_limit"


def test_transcript_stats_handles_missing_file():
    """A worker that just spawned hasn't opened its transcript yet.
    The stats lookup must return zeros, not raise."""
    from stream_eval.monitor.ps import _transcript_stats

    stats = _transcript_stats("/tmp/does-not-exist.jsonl")
    assert stats["total_retries"] == 0
    assert stats["last_error"] is None


def test_find_agent_workers_for_falls_back_to_sidecar_when_no_real_children(tmp_path, monkeypatch):
    """The fake submodule writes a sidecar JSON when scenarios declare
    in-flight workers. find_agent_workers_for falls back to that
    sidecar when psutil yields no real children, so fake harnesses
    can render the in-flight cells without spinning up subprocesses."""
    from stream_eval.monitor.ps import find_agent_workers_for

    fake_state = tmp_path / ".local" / "state" / "stream-eval" / "fake"
    fake_state.mkdir(parents=True)
    sidecar = fake_state / "x.workers.json"
    sidecar.write_text('{"harness_pid": 90001, "workers": ['
                        '{"pid": 9000101, "started_at": 1.0, '
                        '"cmdline": ["claude"], '
                        '"fixture_id": "q0", "run": 1, '
                        '"transcript_path": null, "retries": 2, '
                        '"latest_attempt": 2, "max_retries_field": 10, '
                        '"last_error": "rate_limit"}'
                        ']}')
    monkeypatch.setattr("stream_eval.monitor.ps.Path.home",
                        lambda: tmp_path)
    # Pretend the harness pid exists but has no children.
    parent = _fake_proc(90001, 1, "python3", ["python3"], 0.0)
    parent.children = mock.MagicMock(return_value=[])
    with mock.patch("stream_eval.monitor.ps.psutil.Process",
                    return_value=parent):
        workers = list(find_agent_workers_for(90001))
    assert len(workers) == 1
    assert workers[0]["fixture_id"] == "q0"
    assert workers[0]["retries"] == 2
