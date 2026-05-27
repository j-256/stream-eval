"""Talk to a running harness's Unix-socket control listener.

Used by the dashboard's worker-control routes; could also be used by a
human at a Python REPL for ad-hoc adjustments.

Public surface:
- HarnessSocketClient: opens, sends, reads-back per command.
- SocketClientError: connection or protocol failures.
"""
import os
import socket as _socket


class SocketClientError(Exception):
    """Raised on connect failure, no socket file, or unexpected response."""


class HarnessSocketClient:
    """One client per running harness. Each method opens a fresh
    connection, sends one command, reads the response, closes. We
    deliberately don't keep a persistent connection: simpler error
    handling, and the dashboard does at most a few commands per second.
    """

    def __init__(self, socket_path, timeout=2.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def _send(self, line):
        if not os.path.exists(self.socket_path):
            raise SocketClientError(
                f"socket not found: {self.socket_path}"
            )
        import time as _time
        last_exc = None
        for attempt in range(4):
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            try:
                s.connect(self.socket_path)
                s.sendall((line + "\n").encode("utf-8"))
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                s.sendall(b"QUIT\n")
                return buf.decode("utf-8").strip()
            except ConnectionRefusedError as exc:
                # Socket file exists but the listener hasn't called
                # listen() yet. Retry briefly.
                last_exc = exc
                s.close()
                _time.sleep(0.05 * (attempt + 1))
                continue
            except (ConnectionError, OSError) as exc:
                raise SocketClientError(f"socket error: {exc}") from exc
            finally:
                try:
                    s.close()
                except OSError:
                    pass
        raise SocketClientError(f"socket error: {last_exc}") from last_exc

    def get_workers(self):
        resp = self._send("GET workers")
        if resp.startswith("ERR"):
            raise SocketClientError(resp)
        try:
            return int(resp)
        except ValueError:
            raise SocketClientError(f"unexpected response: {resp!r}")

    def set_workers(self, n):
        resp = self._send(f"SET workers {n}")
        if resp != "OK":
            raise SocketClientError(resp)

    def increment(self):
        self.set_workers(self.get_workers() + 1)

    def decrement(self):
        self.set_workers(max(0, self.get_workers() - 1))

    def pause(self):
        resp = self._send("PAUSE")
        if resp != "OK":
            raise SocketClientError(resp)

    def resume(self):
        resp = self._send("RESUME")
        if resp != "OK":
            raise SocketClientError(resp)

    def get_state(self):
        resp = self._send("GET state")
        if resp.startswith("ERR"):
            raise SocketClientError(resp)
        return resp
