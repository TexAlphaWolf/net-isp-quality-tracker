# Security

## Model

ispq is a local tool: a command line program and a SQLite file. It has **no network service of its own**; it only
opens outgoing TCP, UDP and HTTP(S) connections to the targets you list. It needs no privileges (no ICMP, no raw
sockets) and the Docker image runs as an unprivileged user with a read-only root filesystem and no capabilities.

What it does to limit damage:

- the configuration is validated strictly (unknown keys, ranges, URL scheme); credentials inside a URL are refused,
  so they cannot end up in the database;
- failures are stored as short fixed reasons (`timeout`, `refused`, ...), never as raw exception text or URLs;
- DNS answers are accepted only from the resolver itself (connected UDP socket) and only with the matching query id;
- downloads are opt-in, capped (`max_bytes`) and can run every Nth cycle;
- the HTML report is a single page with no scripts and no external files, and everything that comes from the
  database or the configuration is escaped;
- SQL is parameterised.

What it does not protect: the database holds the names and addresses of your targets and the times you were online.
Treat it like any log file. Do not point it at hosts you are not allowed to probe every minute.

## Reporting

Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab), not in a public issue.
