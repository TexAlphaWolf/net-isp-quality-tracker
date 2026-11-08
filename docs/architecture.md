# Architecture

```text
 config.toml ──► collector ──► probes (tcp, dns, http) ──► store (SQLite) ──► analysis ──► render (md, html, json, influx)
                  every N s       one round per target        one row per round      outages, availability     cli
```

- **probes.py**: each probe returns a `Round`: attempts, successes, and the round-trip time of every success. TCP times the
  handshake; DNS sends a hand-built query on a connected UDP socket (so only the resolver can answer) and accepts only a
  response with the same id, no error and at least one record; HTTP times the first byte and optionally reads the body
  with `read1`, so the speed follows the real arrival of the data.
- **collector.py**: all probes of a cycle run side by side, so a cycle lasts as long as its slowest probe. Cycles are placed on
  a fixed grid; if one overruns, the missed slots are skipped, never run in a burst. SIGINT and SIGTERM stop it cleanly.
- **store.py**: one row per target per cycle, the round-trip times of every attempt kept as JSON so percentiles are exact. A
  cycle is written in one transaction.
- **analysis.py**: the definitions (see the docstring) and the report. A cycle is *down* when every TCP target failed every
  attempt. An outage is two or more down cycles in a row; one is a *blip*. A hole in the data larger than three intervals
  is a *gap*, which is neither up nor down and which an outage never spans.
- **render.py**: Markdown, a single self-contained HTML page with inline SVG charts, and InfluxDB line protocol.
- **cli.py**: exit codes 0 ok, 1 problem found (`once`: down; `report`/`outages`: outages exist), 2 error.

Why TCP and not ICMP: ping needs raw sockets (root) or an external binary whose output differs between systems, and many
networks treat ICMP differently from real traffic. A TCP handshake is what applications experience.
