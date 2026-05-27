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


def test_find_claude_workers_for_rejects_git_subprocess_with_claude_in_path():
    """The runner spawns git for worktree management. A git invocation
    whose --git-dir path happens to contain 'claude' (e.g.
    /Users/me/claude-code-skills/.git) must NOT be reported as a
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
         "--git-dir=/Users/me/claude-code-skills/.git",
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
