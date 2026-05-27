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
