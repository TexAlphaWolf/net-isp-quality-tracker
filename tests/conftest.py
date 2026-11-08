"""Local fake servers: real sockets on loopback, so the probes are exercised for real and nothing leaves the machine."""

from __future__ import annotations

import random
import socket
import struct
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from ispq.store import Store

# Windows takes about two seconds to give up on a closed port, so a short timeout can fire before "refused" arrives.
REFUSED = {"refused", "timeout"} if sys.platform == "win32" else {"refused"}


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def tcp_port() -> Iterator[int]:
    """A listening TCP port that accepts and drops connections."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    srv.settimeout(0.1)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                continue
            c.close()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join()
    srv.close()


@pytest.fixture
def closed_tcp_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


class FakeDns:
    """A resolver whose behaviour the test chooses: ok | nxdomain | servfail | drop | wrong_id_first."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = int(self.sock.getsockname()[1])
        self.mode = "ok"
        self.delay = 0.0
        self.drop_p = 0.0  # drop this share of the queries (seeded, so a run can be repeated)
        self.rng = random.Random(0)
        self.queries = 0
        self.dropped = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _reply(self, data: bytes, rcode: int, answers: int) -> bytes:
        qid = data[:2]
        question = data[12:]
        flags = 0x8180 | rcode
        out = qid + struct.pack("!HHHHH", flags, 1, answers, 0, 0) + question
        if answers:
            out += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + bytes([192, 0, 2, 1])
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(512)
            except OSError:
                continue
            self.queries += 1
            if self.mode == "drop" or (self.drop_p and self.rng.random() < self.drop_p):
                self.dropped += 1
                continue
            threading.Thread(target=self._answer, args=(data, addr), daemon=True).start()  # queries do not queue up

    def _answer(self, data: bytes, addr: tuple[str, int]) -> None:
        time.sleep(self.delay)
        try:
            if self.mode == "nxdomain":
                self.sock.sendto(self._reply(data, 3, 0), addr)
            elif self.mode == "servfail":
                self.sock.sendto(self._reply(data, 2, 0), addr)
            else:
                if self.mode == "wrong_id_first":
                    bad = bytes([data[0] ^ 0xFF, data[1]]) + data[2:]
                    self.sock.sendto(self._reply(bad, 0, 1), addr)
                self.sock.sendto(self._reply(data, 0, 1), addr)
        except OSError:  # the test finished and closed the socket
            pass

    def close(self) -> None:
        self._stop.set()
        self._t.join()
        self.sock.close()


@pytest.fixture
def dns() -> Iterator[FakeDns]:
    d = FakeDns()
    yield d
    d.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence
        pass

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        q = parse_qs(parts.query)
        if parts.path == "/404":
            self.send_error(404)
            return
        if parts.path == "/delay":
            time.sleep(int(q.get("ms", ["0"])[0]) / 1000)
        size = int(q.get("size", ["1000"])[0])
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        chunk = int(q.get("chunk", ["0"])[0])
        pause = int(q.get("pause_ms", ["0"])[0]) / 1000
        sent = 0
        try:
            while sent < size:
                n = min(chunk or size, size - sent)
                self.wfile.write(b"x" * n)
                sent += n
                if pause:
                    time.sleep(pause)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def http_base() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    t.join()
