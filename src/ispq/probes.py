"""The measurements. Standard library only, no privileges needed (no ICMP, no raw sockets).

Each probe returns a `Round`: how many attempts were made, how many worked, and the round-trip time of each one
that did, in milliseconds.
"""

from __future__ import annotations

import errno
import http.client
import secrets
import socket
import ssl
import struct
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ispq.config import DnsTarget, HttpTarget, TcpTarget

CHUNK = 65536


@dataclass
class Round:
    kind: str  # tcp | dns | http
    target: str
    sent: int
    ok: int
    rtts: list[float] = field(default_factory=list)  # milliseconds, successful attempts only
    error: str = ""  # the last failure, short and free of secrets
    bytes: int | None = None  # http with bandwidth
    mbps: float | None = None


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 3)


def _why(exc: BaseException) -> str:
    """A short, stable reason: enough to tell a timeout from a refusal, nothing that could carry a secret."""
    if isinstance(exc, TimeoutError | socket.timeout):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, ConnectionResetError | ConnectionAbortedError | BrokenPipeError):
        return "connection reset"
    if isinstance(exc, ssl.SSLError):
        return "tls error"
    if isinstance(exc, socket.gaierror):
        return "name not resolved"
    if isinstance(exc, OSError):
        code = exc.errno
        if code in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN):
            return "unreachable"
        if code in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE):
            return "connection reset"
        return f"os error {code}" if code is not None else "os error"
    if isinstance(exc, http.client.HTTPException):
        return "http error"
    return type(exc).__name__


def tcp_probe(t: TcpTarget) -> Round:
    """Open `count` TCP connections one after the other and time each handshake."""
    result = Round("tcp", t.name, t.count, 0)
    try:
        infos = socket.getaddrinfo(t.host, t.port, type=socket.SOCK_STREAM)
        family, kind, proto, _, addr = infos[0]
    except (OSError, IndexError) as exc:
        result.error = _why(exc)
        return result
    for i in range(t.count):
        if i and t.gap:
            time.sleep(t.gap)
        sock = socket.socket(family, kind, proto)
        sock.settimeout(t.timeout)
        start = time.perf_counter()
        try:
            sock.connect(addr)
        except OSError as exc:
            result.error = _why(exc)
        else:
            result.rtts.append(_ms(time.perf_counter() - start))
            result.ok += 1
        finally:
            sock.close()
    return result


def build_query(name: str, qid: int) -> bytes:
    """A DNS query for the A record of `name` (RFC 1035)."""
    try:
        labels = name.rstrip(".").encode("idna").split(b".")
    except UnicodeError as exc:
        raise ValueError("not a valid DNS name") from exc
    if any(not lab or len(lab) > 63 for lab in labels) or sum(len(lab) + 1 for lab in labels) > 254:
        raise ValueError("not a valid DNS name")
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # recursion desired, one question
    question = b"".join(bytes([len(lab)]) + lab for lab in labels) + b"\x00" + struct.pack("!HH", 1, 1)
    return header + question


def check_response(data: bytes, qid: int) -> bool:
    """True for a genuine answer to our query: same id, a response, no error, at least one record."""
    if len(data) < 12:
        return False
    rid, flags, _qd, an, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    return rid == qid and bool(flags & 0x8000) and (flags & 0x000F) == 0 and an > 0


def dns_probe(t: DnsTarget) -> Round:
    """Ask the resolver `count` times for the same name and time each answer."""
    result = Round("dns", t.name, t.count, 0)
    try:
        family, kind, proto, _, addr = socket.getaddrinfo(t.server, t.port, type=socket.SOCK_DGRAM)[0]
        build_query(t.query, 0)
    except (OSError, IndexError, ValueError) as exc:
        result.error = _why(exc) if not isinstance(exc, ValueError) else "bad query name"
        return result
    for i in range(t.count):
        if i and t.gap:
            time.sleep(t.gap)
        qid = secrets.randbits(16)
        sock = socket.socket(family, kind, proto)
        try:
            sock.connect(addr)  # a connected UDP socket only accepts datagrams from the resolver itself
            deadline = time.perf_counter() + t.timeout
            start = time.perf_counter()
            sock.send(build_query(t.query, qid))
            while True:
                left = deadline - time.perf_counter()
                if left <= 0:
                    raise TimeoutError
                sock.settimeout(left)
                data = sock.recv(4096)
                if check_response(data, qid):
                    result.rtts.append(_ms(time.perf_counter() - start))
                    result.ok += 1
                    break
                if len(data) >= 2 and struct.unpack("!H", data[:2])[0] == qid:
                    result.error = "resolver returned an error"
                    break  # right id, but NXDOMAIN, SERVFAIL, empty...: a failure, do not wait for more
        except OSError as exc:
            result.error = _why(exc)
        finally:
            sock.close()
    return result


def http_probe(t: HttpTarget) -> Round:
    """One GET. Time to the first byte of the response; optionally the download speed of the body."""
    result = Round("http", t.name, 1, 0)
    parts = urlsplit(t.url)
    host = parts.hostname or ""
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    conn: http.client.HTTPConnection
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(host, parts.port, timeout=t.timeout, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, parts.port, timeout=t.timeout)
    try:
        start = time.perf_counter()
        conn.request("GET", path, headers={"User-Agent": "ispq", "Accept-Encoding": "identity"})
        response = conn.getresponse()
        first = time.perf_counter() - start
        if response.status >= 400:
            result.error = f"http {response.status}"
            return result
        result.ok, result.rtts = 1, [_ms(first)]
        if t.bandwidth:
            # Speed is measured from the arrival of the first data onwards: that first piece comes with the
            # headers and costs no time, so counting it would overstate the rate. read1 returns whatever has
            # arrived, so the clock follows the real arrival of the data and not the size of our reads.
            head = response.read1(min(CHUNK, t.max_bytes))
            got = len(head)
            body_start = time.perf_counter()
            while head and got < t.max_bytes:
                chunk = response.read1(min(CHUNK, t.max_bytes - got))
                if not chunk:
                    break
                got += len(chunk)
            elapsed = time.perf_counter() - body_start
            result.bytes = got
            if got > len(head) and elapsed > 0.001:
                result.mbps = round((got - len(head)) * 8 / elapsed / 1e6, 3)
    except (OSError, http.client.HTTPException) as exc:
        result.error = _why(exc)
        result.ok, result.rtts = 0, []
    finally:
        conn.close()
    return result
