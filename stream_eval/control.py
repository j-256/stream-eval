"""Signal-handler and Unix-socket layer for stream-eval.

Mutates the currently-running Dispatcher's target_workers / state from
outside the harness. Three control surfaces:

1. SIGUSR1: target_workers -= 1 (floor 0).
2. SIGUSR2: target_workers += 1.
3. Unix socket at /tmp/stream-eval-<pid>.sock, line protocol:
   GET workers          -> "<n>\n"
   SET workers <n>      -> "OK\n"
   PAUSE                -> "OK\n"
   RESUME               -> "OK\n"
   GET state            -> "<state>\n"
   QUIT                 -> closes the connection

Public surface:
- install_signal_handlers(): wire SIGUSR1/SIGUSR2.
- serve_socket(socket_path): blocking listener; runs in a daemon thread.
"""
import os
import signal
import socket
import threading

from stream_eval.runner import get_current_dispatcher


def _adjust_target(delta):
    d = get_current_dispatcher()
    if d is None:
        return
    d.target_workers = d.target_workers + delta


def _on_sigusr1(_signum, _frame):
    _adjust_target(-1)


def _on_sigusr2(_signum, _frame):
    _adjust_target(+1)


def install_signal_handlers():
    """Install SIGUSR1/SIGUSR2 handlers. Idempotent: safe to call twice."""
    signal.signal(signal.SIGUSR1, _on_sigusr1)
    signal.signal(signal.SIGUSR2, _on_sigusr2)


def serve_socket(socket_path):
    """Block, accepting connections on socket_path. One command per line,
    response per command. Caller runs this in a daemon thread.

    Returns when the socket is closed."""
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(socket_path)
    sock.listen(4)
    sock.settimeout(0.5)

    try:
        while True:
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            with conn:
                _handle_conn(conn)
    finally:
        try:
            sock.close()
        finally:
            if os.path.exists(socket_path):
                os.unlink(socket_path)


def _handle_conn(conn):
    buf = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            response = _handle_line(line.decode("utf-8", errors="replace").strip())
            if response is None:
                return
            conn.sendall((response + "\n").encode("utf-8"))


def _handle_line(line):
    """Handle one command line. Returns the response string, or None to
    close the connection (QUIT)."""
    parts = line.split()
    if not parts:
        return "ERR empty"
    cmd = parts[0].upper()

    d = get_current_dispatcher()
    if d is None and cmd != "QUIT":
        return "ERR no dispatcher"

    if cmd == "QUIT":
        return None
    if cmd == "GET":
        if len(parts) >= 2 and parts[1].lower() == "workers":
            return str(d.target_workers)
        if len(parts) >= 2 and parts[1].lower() == "state":
            return d.state.value
        return "ERR usage: GET workers | GET state"
    if cmd == "SET":
        if len(parts) >= 3 and parts[1].lower() == "workers":
            try:
                d.target_workers = int(parts[2])
                return "OK"
            except ValueError:
                return f"ERR invalid integer: {parts[2]!r}"
        return "ERR usage: SET workers <n>"
    if cmd == "PAUSE":
        d.pause()
        return "OK"
    if cmd == "RESUME":
        d.resume()
        return "OK"
    return f"ERR unknown command: {cmd!r}"
