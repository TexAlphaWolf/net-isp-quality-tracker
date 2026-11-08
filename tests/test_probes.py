from __future__ import annotations

import errno
import socket
import ssl

import pytest

from conftest import REFUSED, FakeDns
from ispq import probes
from ispq.config import DnsTarget, HttpTarget, TcpTarget


def tcp(port: int, **kw: object) -> TcpTarget:
    return TcpTarget("t", "127.0.0.1", port, **{"count": 4, "timeout": 1.0, "gap": 0.0, **kw})  # type: ignore[arg-type]


def test_tcp_open_port(tcp_port: int) -> None:
    r = probes.tcp_probe(tcp(tcp_port))
    assert (r.kind, r.sent, r.ok, len(r.rtts), r.error) == ("tcp", 4, 4, 4, "")
    assert all(0 < x < 500 for x in r.rtts)


def test_tcp_closed_port_is_loss_with_a_reason(closed_tcp_port: int) -> None:
    r = probes.tcp_probe(tcp(closed_tcp_port))
    assert (r.sent, r.ok, r.rtts) == (4, 0, []) and r.error in REFUSED


def test_tcp_name_that_does_not_resolve() -> None:
    r = probes.tcp_probe(TcpTarget("t", "no-such-host.invalid", 443, count=3))
    assert (r.sent, r.ok, r.error) == (3, 0, "name not resolved")


def test_tcp_ipv6_loopback_if_available() -> None:
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    try:
        srv = socket.socket(socket.AF_INET6)
        srv.bind(("::1", 0))
    except OSError:
        pytest.skip("no IPv6 loopback")
    srv.listen(8)
    try:
        r = probes.tcp_probe(TcpTarget("t", "::1", srv.getsockname()[1], count=2, gap=0))
        assert r.ok == 2
    finally:
        srv.close()


def test_tcp_timeout_is_reported_as_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    class Hung:
        def __init__(self, *a: object) -> None:
            pass

        def settimeout(self, _t: float) -> None:
            pass

        def connect(self, _addr: object) -> None:
            raise TimeoutError

        def close(self) -> None:
            pass

    monkeypatch.setattr(probes.socket, "socket", Hung)
    r = probes.tcp_probe(TcpTarget("t", "127.0.0.1", 9, count=3, gap=0))
    assert (r.ok, r.error) == (0, "timeout")


def test_tcp_gap_between_attempts_is_honoured(tcp_port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(probes.time, "sleep", sleeps.append)
    probes.tcp_probe(tcp(tcp_port, count=3, gap=0.25))
    assert sleeps == [0.25, 0.25]  # between attempts, not before the first


@pytest.mark.parametrize(
    "exc, text",
    [
        (TimeoutError(), "timeout"),
        (ConnectionRefusedError(), "refused"),
        (ssl.SSLError("x"), "tls error"),
        (socket.gaierror(), "name not resolved"),
        (OSError(errno.ENETUNREACH, "x"), "unreachable"),
        (OSError(errno.EHOSTUNREACH, "x"), "unreachable"),
        (ConnectionResetError(), "connection reset"),
        (OSError(errno.EACCES, "x"), f"os error {errno.EACCES}"),
        (OSError("bare"), "os error"),
        (KeyError("k"), "KeyError"),
    ],
)
def test_failure_reasons_are_short_and_stable(exc: BaseException, text: str) -> None:
    assert probes._why(exc) == text


# ---- DNS ----


def test_build_query_layout() -> None:
    q = probes.build_query("www.example.com", 0xABCD)
    assert q[:2] == b"\xab\xcd" and q[2:4] == b"\x01\x00" and q[4:6] == b"\x00\x01"
    assert q[12:] == b"\x03www\x07example\x03com\x00\x00\x01\x00\x01"
    assert probes.build_query("example.com.", 1)[12:] == b"\x07example\x03com\x00\x00\x01\x00\x01"  # trailing dot
    assert b"xn--" in probes.build_query("münchen.de", 1)  # internationalised names


@pytest.mark.parametrize("name", ["", "a..b", "x" * 64 + ".com", ".".join(["a" * 60] * 5)])
def test_build_query_rejects_bad_names(name: str) -> None:
    with pytest.raises(ValueError, match="valid DNS name"):
        probes.build_query(name, 1)


def reply(qid: int, flags: int, answers: int) -> bytes:
    import struct

    return struct.pack("!HHHHHH", qid, flags, 1, answers, 0, 0)


def test_check_response_matrix() -> None:
    assert probes.check_response(reply(7, 0x8180, 1), 7)
    assert not probes.check_response(reply(8, 0x8180, 1), 7)  # someone else's answer
    assert not probes.check_response(reply(7, 0x0100, 1), 7)  # not a response
    assert not probes.check_response(reply(7, 0x8183, 0), 7)  # NXDOMAIN
    assert not probes.check_response(reply(7, 0x8182, 0), 7)  # SERVFAIL
    assert not probes.check_response(reply(7, 0x8180, 0), 7)  # no records
    assert not probes.check_response(b"short", 7) and not probes.check_response(b"", 7)


def dns_target(d: FakeDns, **kw: object) -> DnsTarget:
    return DnsTarget("d", "127.0.0.1", "example.com", d.port, **{"count": 3, "timeout": 0.5, "gap": 0.0, **kw})  # type: ignore[arg-type]


def test_dns_measures_the_answer_time(dns: FakeDns) -> None:
    dns.delay = 0.05
    r = probes.dns_probe(dns_target(dns))
    assert (r.kind, r.sent, r.ok, r.error) == ("dns", 3, 3, "")
    assert all(45 <= x < 300 for x in r.rtts)  # at least the delay the server added


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("nxdomain", "resolver returned an error"), ("servfail", "resolver returned an error"), ("drop", "timeout")],
)
def test_dns_failures(dns: FakeDns, mode: str, reason: str) -> None:
    dns.mode = mode
    r = probes.dns_probe(dns_target(dns, count=2, timeout=0.2))
    assert (r.ok, r.rtts, r.error) == (0, [], reason)


def test_dns_ignores_an_answer_with_the_wrong_id(dns: FakeDns) -> None:
    dns.mode = "wrong_id_first"
    r = probes.dns_probe(dns_target(dns))
    assert r.ok == 3


def test_dns_nothing_listening() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = probes.dns_probe(DnsTarget("d", "127.0.0.1", "example.com", port, count=2, timeout=0.5, gap=0))
    assert r.ok == 0 and r.error in {"refused", "timeout", "connection reset"}  # Windows reports ICMP unreachable as a reset


def test_dns_bad_inputs() -> None:
    assert probes.dns_probe(DnsTarget("d", "127.0.0.1", "a..b", 5300, count=2)).error == "bad query name"
    assert probes.dns_probe(DnsTarget("d", "no-such-host.invalid", "example.com", count=2)).error == "name not resolved"


# ---- HTTP ----


def ht(url: str, **kw: object) -> HttpTarget:
    return HttpTarget("h", url, **{"timeout": 2.0, **kw})  # type: ignore[arg-type]


def test_http_time_to_first_byte(http_base: str) -> None:
    r = probes.http_probe(ht(f"{http_base}/delay?ms=80"))
    assert (r.kind, r.sent, r.ok, r.error, r.bytes, r.mbps) == ("http", 1, 1, "", None, None)
    assert 75 <= r.rtts[0] < 600


def test_http_error_status_is_a_failure(http_base: str) -> None:
    r = probes.http_probe(ht(f"{http_base}/404"))
    assert (r.ok, r.rtts, r.error) == (0, [], "http 404")


def test_http_bandwidth_counts_the_bytes_and_respects_the_cap(http_base: str) -> None:
    r = probes.http_probe(
        ht(f"{http_base}/ok?size=400000&chunk=50000&pause_ms=10", bandwidth=True, max_bytes=1_000_000)
    )
    assert r.ok == 1 and r.bytes == 400000 and r.mbps is not None and r.mbps > 0
    capped = probes.http_probe(ht(f"{http_base}/ok?size=400000", bandwidth=True, max_bytes=100_000))
    assert capped.bytes == 100_000  # never reads more than it was allowed to


def test_http_bandwidth_tracks_a_throttled_server(http_base: str) -> None:
    # 20000 bytes every 50 ms = 3.2 Mbit/s offered. The upper bound is physical (the first read may be a partial
    # chunk, so up to ~3.6 is possible); the lower bound only has to survive a stalled shared CI runner. The
    # precise accuracy is measured in tools/evaluate.py, not asserted against a wall clock here.
    r = probes.http_probe(ht(f"{http_base}/ok?size=200000&chunk=20000&pause_ms=50", bandwidth=True))
    assert r.mbps is not None and 0.3 < r.mbps < 4.0


def test_http_connection_failures(closed_tcp_port: int) -> None:
    r = probes.http_probe(ht(f"http://127.0.0.1:{closed_tcp_port}/"))
    assert r.ok == 0 and r.error in REFUSED
    assert probes.http_probe(ht("http://no-such-host.invalid/")).error == "name not resolved"


def test_https_against_a_plain_server_fails_cleanly(http_base: str) -> None:
    r = probes.http_probe(ht(http_base.replace("http://", "https://") + "/"))
    assert r.ok == 0 and r.error in {"tls error", "connection reset", "http error", "timeout"}
