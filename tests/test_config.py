from __future__ import annotations

from pathlib import Path

import pytest

from ispq import config
from ispq.config import ConfigError, parse


def test_the_default_configuration_is_valid() -> None:
    cfg = config.load(None)
    assert cfg.interval == 60 and len(cfg.tcp) == 3 and len(cfg.dns) == 1 and cfg.http == ()
    assert cfg.targets() == ["cloudflare", "google", "quad9", "dns-cloudflare"]


def test_the_example_file_is_valid() -> None:
    cfg = config.load(Path(__file__).resolve().parent.parent / "examples" / "ispq.toml")
    assert cfg.targets()


def test_full_example() -> None:
    cfg = parse(
        {
            "interval": 30,
            "database": "x.db",
            "tcp": [{"name": "a", "host": "example.net", "port": 80, "count": 3, "timeout": 1.5, "gap": 0}],
            "dns": [{"name": "d", "server": "9.9.9.9", "query": "example.org", "port": 5353}],
            "http": [
                {
                    "name": "h",
                    "url": "https://example.net/f.bin",
                    "bandwidth": True,
                    "every": 10,
                    "max_bytes": 1_000_000,
                }
            ],
        }
    )
    assert cfg.tcp[0].port == 80 and cfg.dns[0].port == 5353 and cfg.http[0].bandwidth and cfg.http[0].every == 10


@pytest.mark.parametrize(
    "data, message",
    [
        ({}, "nothing to measure"),
        ({"tcp": [{"name": "a", "host": "h"}], "bogus": 1}, "unknown top-level"),
        ({"tcp": [{"name": "a", "host": "h", "prot": 80}]}, "unknown key"),
        ({"interval": 1, "tcp": [{"name": "a", "host": "h"}]}, "interval"),
        ({"interval": "60", "tcp": [{"name": "a", "host": "h"}]}, "interval"),
        ({"interval": True, "tcp": [{"name": "a", "host": "h"}]}, "interval"),
        ({"tcp": [{"name": "a", "host": "h", "port": 0}]}, "port"),
        ({"tcp": [{"name": "a", "host": "h", "port": 70000}]}, "port"),
        ({"tcp": [{"name": "a", "host": "h", "count": 0}]}, "count"),
        ({"tcp": [{"name": "a", "host": "h", "timeout": 0}]}, "timeout"),
        ({"tcp": [{"name": "a", "host": "h", "count": 2.5}]}, "count"),
        ({"tcp": [{"host": "h"}]}, "name"),
        ({"tcp": [{"name": "a"}]}, "host"),
        ({"tcp": [{"name": "a\nb", "host": "h"}]}, "name"),
        ({"tcp": [{"name": "a" * 100, "host": "h"}]}, "name"),
        ({"tcp": [{"name": "a", "host": "h"}, {"name": "a", "host": "g"}]}, "unique"),
        ({"tcp": [{"name": "a", "host": "h"}], "dns": [{"name": "a", "server": "s"}]}, "unique"),
        ({"http": [{"name": "h", "url": "ftp://x/y"}]}, "http"),
        ({"http": [{"name": "h", "url": "http://"}]}, "http"),
        ({"http": [{"name": "h", "url": "http://user:pw@x/y"}]}, "credentials"),
        ({"http": [{"name": "h", "url": "http://x/y", "bandwidth": "yes"}]}, "bandwidth"),
        ({"http": [{"name": "h", "url": "http://x/y", "max_bytes": 10**10}]}, "max_bytes"),
        ({"tcp": "nope"}, "list of tables"),
        ({"tcp": ["nope"]}, "list of tables"),
        ({"database": "", "tcp": [{"name": "a", "host": "h"}]}, "database"),
    ],
)
def test_invalid_configurations(data: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(data)


def test_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        config.load(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[[tcp\nname=", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        config.load(bad)
