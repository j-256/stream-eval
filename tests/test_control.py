"""Tests for stream_eval.control: signal handlers and Unix-socket
listener that mutate the current Dispatcher."""
import os
import signal
import socket
import threading
import time
from unittest import mock

import pytest

from stream_eval.control import (
    install_signal_handlers,
    serve_socket,
)


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    """install_signal_handlers() mutates process-level signal state.
    Snapshot SIGUSR1/SIGUSR2 before each test and restore after, so a
    test's installed handler doesn't leak into subsequent tests or
    pytest's own machinery."""
    prior_usr1 = signal.getsignal(signal.SIGUSR1)
    prior_usr2 = signal.getsignal(signal.SIGUSR2)
    try:
        yield
    finally:
        signal.signal(signal.SIGUSR1, prior_usr1)
        signal.signal(signal.SIGUSR2, prior_usr2)


class _StubDispatcher:
    """Stand-in for a real Dispatcher used in control-layer tests."""
    def __init__(self):
        self.target_workers = 4
        self._paused = False
        self._stopped = False
    def pause(self):
        self._paused = True
    def resume(self):
        self._paused = False
    def stop(self):
        self._stopped = True
    @property
    def state(self):
        from stream_eval.pool import DispatcherState
        if self._stopped:
            return DispatcherState.STOPPED
        return DispatcherState.PAUSED if self._paused else DispatcherState.RUNNING


def test_sigusr1_decrements_target_workers():
    d = _StubDispatcher()
    with mock.patch("stream_eval.control.get_current_dispatcher", return_value=d):
        install_signal_handlers()
        os.kill(os.getpid(), signal.SIGUSR1)
        time.sleep(0.05)  # signal delivery is async
    assert d.target_workers == 3


def test_sigusr2_increments_target_workers():
    d = _StubDispatcher()
    with mock.patch("stream_eval.control.get_current_dispatcher", return_value=d):
        install_signal_handlers()
        os.kill(os.getpid(), signal.SIGUSR2)
        time.sleep(0.05)
    assert d.target_workers == 5


def test_signal_when_no_dispatcher_is_noop():
    """If no eval is running, the signal handler must not raise."""
    with mock.patch("stream_eval.control.get_current_dispatcher", return_value=None):
        install_signal_handlers()
        os.kill(os.getpid(), signal.SIGUSR1)
        time.sleep(0.05)
    # No exception = pass.


def _send_socket_command(sock_path, cmd):
    """Open a Unix socket, send one command line, read the response."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    try:
        s.sendall((cmd + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.sendall(b"QUIT\n")
        return buf.decode("utf-8").strip()
    finally:
        s.close()


@pytest.fixture
def socket_server():
    # Unix socket paths must be short (<=104 bytes on macOS).
    # pytest's tmp_path lives under /private/var/folders/... which exceeds
    # the AF_UNIX path limit, so we use /tmp directly with a unique name.
    import tempfile
    sock_path = tempfile.mktemp(prefix="se_test_", suffix=".sock", dir="/tmp")
    d = _StubDispatcher()

    with mock.patch("stream_eval.control.get_current_dispatcher", return_value=d):
        server_thread = threading.Thread(
            target=serve_socket, args=(sock_path,), daemon=True
        )
        server_thread.start()
        # Wait for the socket to exist.
        for _ in range(20):
            if os.path.exists(sock_path):
                break
            time.sleep(0.05)
        assert os.path.exists(sock_path), "socket never appeared"
        yield (sock_path, d)
    # serve_socket removes the socket file on exit (daemon thread cleanup).


def test_socket_get_workers(socket_server):
    sock_path, d = socket_server
    assert _send_socket_command(sock_path, "GET workers") == "4"


def test_socket_set_workers(socket_server):
    sock_path, d = socket_server
    assert _send_socket_command(sock_path, "SET workers 8") == "OK"
    assert d.target_workers == 8


def test_socket_pause_resume(socket_server):
    sock_path, d = socket_server
    assert _send_socket_command(sock_path, "PAUSE") == "OK"
    assert d._paused is True
    assert _send_socket_command(sock_path, "GET state") == "paused"
    assert _send_socket_command(sock_path, "RESUME") == "OK"
    assert d._paused is False


def test_socket_unknown_command(socket_server):
    sock_path, _d = socket_server
    response = _send_socket_command(sock_path, "FROBNICATE")
    assert response.startswith("ERR")


def test_socket_survives_abrupt_client_disconnect(socket_server):
    """An abrupt client disconnect (close before reading the response)
    must not kill the listener: the next client must still get served.

    Regression for the case where a BrokenPipeError out of sendall
    inside _handle_conn would propagate up through `with conn` and
    out of the accept loop, taking down the daemon thread and
    permanently disabling the control surface.
    """
    sock_path, d = socket_server

    # Connect, send a command, close before reading the response. On the
    # server side this raises BrokenPipeError when sendall tries to
    # write back to the closed socket.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall(b"GET workers\n")
    s.close()

    # Give the server time to discover the broken pipe.
    time.sleep(0.1)

    # The next client must still get served.
    assert _send_socket_command(sock_path, "GET workers") == "4"
