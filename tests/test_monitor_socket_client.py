"""Tests for stream_eval.monitor.socket_client. Use a real Unix socket
running stream_eval.control.serve_socket against a stub dispatcher."""
import os
import signal
import socket
import tempfile
import threading
import time
from unittest import mock

import pytest

from stream_eval.control import serve_socket
from stream_eval.monitor.socket_client import (
    HarnessSocketClient,
    SocketClientError,
)


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    """Snapshot SIGUSR1/SIGUSR2 so install_signal_handlers (called in
    other test modules' fixtures) doesn't leak handlers across the
    test run."""
    prior_usr1 = signal.getsignal(signal.SIGUSR1)
    prior_usr2 = signal.getsignal(signal.SIGUSR2)
    try:
        yield
    finally:
        signal.signal(signal.SIGUSR1, prior_usr1)
        signal.signal(signal.SIGUSR2, prior_usr2)


class _StubDispatcher:
    def __init__(self):
        self.target_workers = 4
        self._paused = False
    def pause(self):
        self._paused = True
    def resume(self):
        self._paused = False
    @property
    def state(self):
        from stream_eval.pool import DispatcherState
        return DispatcherState.PAUSED if self._paused else DispatcherState.RUNNING


@pytest.fixture
def harness_socket():
    """Spawn a real serve_socket in a daemon thread and yield the path.
    Uses tempfile.mktemp under /tmp so the AF_UNIX path stays under
    macOS's 104-byte limit."""
    sock_path = tempfile.mktemp(prefix="se_test_", suffix=".sock", dir="/tmp")
    d = _StubDispatcher()
    with mock.patch("stream_eval.control.get_current_dispatcher",
                    return_value=d):
        threading.Thread(
            target=serve_socket, args=(sock_path,), daemon=True,
        ).start()
        for _ in range(20):
            if os.path.exists(sock_path):
                break
            time.sleep(0.05)
        yield (sock_path, d)


def test_socket_client_get_workers(harness_socket):
    sock_path, _d = harness_socket
    client = HarnessSocketClient(sock_path)
    assert client.get_workers() == 4


def test_socket_client_set_workers(harness_socket):
    sock_path, d = harness_socket
    client = HarnessSocketClient(sock_path)
    client.set_workers(7)
    assert d.target_workers == 7


def test_socket_client_increment_decrement(harness_socket):
    sock_path, d = harness_socket
    client = HarnessSocketClient(sock_path)
    client.increment()
    assert d.target_workers == 5
    client.decrement()
    assert d.target_workers == 4


def test_socket_client_pause_resume(harness_socket):
    sock_path, d = harness_socket
    client = HarnessSocketClient(sock_path)
    client.pause()
    assert d._paused
    client.resume()
    assert not d._paused


def test_socket_client_raises_when_socket_missing(tmp_path):
    client = HarnessSocketClient(str(tmp_path / "nonexistent.sock"))
    with pytest.raises(SocketClientError):
        client.get_workers()
