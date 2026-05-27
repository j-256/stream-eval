"""In-memory fake of the harness's Unix-socket control listener.

Mirrors the line protocol implemented in stream_eval.control:
  GET workers  -> "<n>\\n"
  SET workers <n>  -> "OK\\n"
  PAUSE  -> "OK\\n"
  RESUME -> "OK\\n"
  GET state  -> "<state>\\n"
  QUIT  -> closes the connection

The fake holds a tiny per-listener state dict {target_workers, state}
and mutates it in response to commands. Each GET workers returns the
current value; SET workers updates it. So clicking +1 on the dashboard
five times bumps the displayed count from N to N+5, exactly as if a
real Dispatcher were behind the socket.

Public surface:
- FakeSocketServer(socket_path, initial_workers, initial_state):
  start a daemon listener thread bound to socket_path. close() to
  shut it down and unlink the socket file.
"""
import os
import socket as _socket
import threading


class FakeSocketServer:
    """Daemon-thread AF_UNIX listener that simulates a harness's
    control socket. Constructor starts the thread; close() stops it
    and removes the socket file."""

    def __init__(self, socket_path, *, initial_workers=2,
                 initial_state="running"):
        self.socket_path = socket_path
        self._target_workers = initial_workers
        self._state = initial_state
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sock = None
        self._thread = threading.Thread(
            target=self._serve, daemon=True,
            name=f"fake-sock-{os.path.basename(socket_path)}",
        )
        self._thread.start()
        # Give the listener a moment to bind so dashboard clients that
        # connect immediately don't get ECONNREFUSED.
        self._ready = threading.Event()
        self._ready.wait(timeout=1.0)

    def _serve(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(4)
        self._sock.settimeout(0.2)
        self._ready.set()
        try:
            while not self._stop.is_set():
                try:
                    conn, _addr = self._sock.accept()
                except _socket.timeout:
                    continue
                except OSError:
                    # Sock closed under us during shutdown.
                    return
                try:
                    with conn:
                        self._handle_conn(conn)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    continue
        finally:
            try:
                self._sock.close()
            finally:
                if os.path.exists(self.socket_path):
                    os.unlink(self.socket_path)

    def _handle_conn(self, conn):
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except _socket.timeout:
                continue
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                response = self._handle_line(
                    line.decode("utf-8", errors="replace").strip()
                )
                if response is None:
                    return
                conn.sendall((response + "\n").encode("utf-8"))

    def _handle_line(self, line):
        parts = line.split()
        if not parts:
            return "ERR empty"
        cmd = parts[0].upper()
        if cmd == "QUIT":
            return None
        with self._lock:
            if cmd == "GET":
                if len(parts) >= 2 and parts[1].lower() == "workers":
                    return str(self._target_workers)
                if len(parts) >= 2 and parts[1].lower() == "state":
                    return self._state
                return "ERR usage: GET workers | GET state"
            if cmd == "SET":
                if len(parts) >= 3 and parts[1].lower() == "workers":
                    try:
                        self._target_workers = max(0, int(parts[2]))
                        return "OK"
                    except ValueError:
                        return f"ERR invalid integer: {parts[2]!r}"
                return "ERR usage: SET workers <n>"
            if cmd == "PAUSE":
                self._state = "paused"
                return "OK"
            if cmd == "RESUME":
                self._state = "running"
                return "OK"
        return f"ERR unknown command: {cmd!r}"

    def close(self):
        self._stop.set()
        # Poke ourselves with a connect so the accept timeout doesn't
        # delay shutdown by a full poll interval.
        try:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(0.1)
            try:
                s.connect(self.socket_path)
            except OSError:
                pass
            finally:
                s.close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)
