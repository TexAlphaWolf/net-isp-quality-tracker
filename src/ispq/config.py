"""Configuration: a small TOML file, validated strictly (an unknown key is an error, not a silent typo)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TcpTarget:
    name: str
    host: str
    port: int = 443
    count: int = 5
    timeout: float = 2.0
    gap: float = 0.2


@dataclass(frozen=True)
class DnsTarget:
    name: str
    server: str
    query: str = "example.com"
    port: int = 53
    count: int = 3
    timeout: float = 2.0
    gap: float = 0.2


@dataclass(frozen=True)
class HttpTarget:
    name: str
    url: str
    timeout: float = 5.0
    every: int = 1  # run on every Nth cycle (a download every minute would use a lot of data)
    bandwidth: bool = False
    max_bytes: int = 5_000_000


@dataclass(frozen=True)
class Config:
    interval: float = 60.0
    database: str = "ispq.db"
    tcp: tuple[TcpTarget, ...] = ()
    dns: tuple[DnsTarget, ...] = ()
    http: tuple[HttpTarget, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def targets(self) -> list[str]:
        return [*(t.name for t in self.tcp), *(t.name for t in self.dns), *(t.name for t in self.http)]


DEFAULT_TOML = """\
# ispq: what to measure. Times are in seconds.
interval = 60                 # one measurement cycle per minute
database = "ispq.db"

# Reachability and latency: how long a TCP connection takes to open.
[[tcp]]
name = "cloudflare"
host = "1.1.1.1"
port = 443

[[tcp]]
name = "google"
host = "8.8.8.8"
port = 53

[[tcp]]
name = "quad9"
host = "9.9.9.9"
port = 443

# Name resolution: how long a DNS query takes.
[[dns]]
name = "dns-cloudflare"
server = "1.1.1.1"
query = "example.com"

# Optional, off by default: page fetch time and download speed.
# [[http]]
# name = "speed"
# url = "https://speed.example.net/10MB.bin"
# bandwidth = true
# every = 60                  # once an hour with a 60 s interval
"""

_KEYS: dict[str, set[str]] = {
    "tcp": {"name", "host", "port", "count", "timeout", "gap"},
    "dns": {"name", "server", "query", "port", "count", "timeout", "gap"},
    "http": {"name", "url", "timeout", "every", "bandwidth", "max_bytes"},
}


def _check_keys(section: str, raw: dict[str, Any]) -> None:
    unknown = set(raw) - _KEYS[section]
    if unknown:
        raise ConfigError(f"[[{section}]] {raw.get('name', '?')!r}: unknown key(s) {', '.join(sorted(unknown))}")


def _num(raw: dict[str, Any], key: str, default: float, lo: float, hi: float, what: str) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not lo <= value <= hi:
        raise ConfigError(f"{what}: {key} must be a number between {lo:g} and {hi:g}")
    return float(value)


def _int(raw: dict[str, Any], key: str, default: int, lo: int, hi: int, what: str) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ConfigError(f"{what}: {key} must be a whole number between {lo} and {hi}")
    return value


def _text(raw: dict[str, Any], key: str, what: str, maxlen: int = 253) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maxlen or any(ord(c) < 32 for c in value):
        raise ConfigError(f"{what}: {key} is required and must be short plain text")
    return value.strip()


def parse(data: dict[str, Any]) -> Config:
    unknown = set(data) - {"interval", "database", "tcp", "dns", "http"}
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    interval = _num(data, "interval", 60.0, 5, 86400, "config")
    database = data.get("database", "ispq.db")
    if not isinstance(database, str) or not database:
        raise ConfigError("config: database must be a path")
    tcp: list[TcpTarget] = []
    dns: list[DnsTarget] = []
    http: list[HttpTarget] = []
    for section, items in (("tcp", data.get("tcp", [])), ("dns", data.get("dns", [])), ("http", data.get("http", []))):
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ConfigError(f"[[{section}]] must be a list of tables")
        for raw in items:
            _check_keys(section, raw)
            name = _text(raw, "name", f"[[{section}]]", 60)
            what = f"[[{section}]] {name!r}"
            if section == "tcp":
                tcp.append(
                    TcpTarget(
                        name,
                        _text(raw, "host", what),
                        _int(raw, "port", 443, 1, 65535, what),
                        _int(raw, "count", 5, 1, 50, what),
                        _num(raw, "timeout", 2.0, 0.1, 60, what),
                        _num(raw, "gap", 0.2, 0, 10, what),
                    )
                )
            elif section == "dns":
                dns.append(
                    DnsTarget(
                        name,
                        _text(raw, "server", what),
                        _text(raw, "query", what) if "query" in raw else "example.com",
                        _int(raw, "port", 53, 1, 65535, what),
                        _int(raw, "count", 3, 1, 50, what),
                        _num(raw, "timeout", 2.0, 0.1, 60, what),
                        _num(raw, "gap", 0.2, 0, 10, what),
                    )
                )
            else:
                url = _text(raw, "url", what, 2000)
                parts = urlsplit(url)
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    raise ConfigError(f"{what}: url must be http:// or https://")
                if parts.username or parts.password:
                    raise ConfigError(f"{what}: credentials in a url would end up in the database; leave them out")
                bandwidth = raw.get("bandwidth", False)
                if not isinstance(bandwidth, bool):
                    raise ConfigError(f"{what}: bandwidth must be true or false")
                http.append(
                    HttpTarget(
                        name,
                        url,
                        _num(raw, "timeout", 5.0, 0.1, 120, what),
                        _int(raw, "every", 1, 1, 100000, what),
                        bandwidth,
                        _int(raw, "max_bytes", 5_000_000, 1000, 100_000_000, what),
                    )
                )
    names = [*(t.name for t in tcp), *(t.name for t in dns), *(t.name for t in http)]
    if len(set(names)) != len(names):
        raise ConfigError("target names must be unique")
    if not names:
        raise ConfigError("nothing to measure: add at least one [[tcp]], [[dns]] or [[http]] target")
    return Config(interval, database, tuple(tcp), tuple(dns), tuple(http))


def load(path: str | Path | None) -> Config:
    """Read a config file; without a path, use the built-in default (the public resolvers)."""
    if path is None:
        return parse(tomllib.loads(DEFAULT_TOML))
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        return parse(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc
