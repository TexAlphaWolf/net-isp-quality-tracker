# Evaluation

Produced by `python tools/evaluate.py` and stored in `report.json` (machine: Linux x86_64, Python 3.13.16).

**What this is and is not.** There is no real ISP in a test environment, so every number below comes from a situation
where the truth is known: servers on loopback that add a chosen delay, drop a chosen share of queries or throttle to a
chosen rate, and simulated months with outages that were injected at known times. It shows that the measurement code
and the analysis do what they claim. It says nothing about any particular provider. Loopback has no real network
noise, so the errors below are the floor of the instrument, not what you will see on the internet.

## 1. Do the probes measure what they say?

### DNS answer time (the server waits a known time before answering; 40 queries each)

| injected delay | measured median | median error | worst error |
|---|---|---|---|
| 0 ms | 0.25 ms | 0.25 ms | 0.73 ms |
| 10 ms | 10.62 ms | 0.62 ms | 2.39 ms |
| 25 ms | 25.76 ms | 0.76 ms | 1.01 ms |
| 50 ms | 50.87 ms | 0.87 ms | 1.27 ms |
| 100 ms | 100.84 ms | 0.84 ms | 2.55 ms |
| 200 ms | 200.95 ms | 0.95 ms | 3.04 ms |

The median error is under a millisecond: that is what the probe adds on its own.

### Loss (the server silently drops a random share of 400 queries, seeded)

| nominal loss | dropped by the server | reported lost | reported loss |
|---|---|---|---|
| 0 % | 0 of 400 | 0 | 0.00 % |
| 5 % | 17 of 400 | 17 | 4.25 % |
| 20 % | 72 of 400 | 72 | 18.00 % |
| 50 % | 197 of 400 | 197 | 49.25 % |

The number of lost queries the probe reports is exactly the number the server dropped. The percentage differs from the
nominal one only because the random drops of a finite sample differ from the nominal share.

### HTTP time to first byte (the server waits a known time)

| injected delay | measured median | median error |
|---|---|---|
| 0 ms | 0.65 ms | 0.65 ms |
| 50 ms | 51.55 ms | 1.55 ms |
| 200 ms | 201.59 ms | 1.59 ms |

### Download speed (the server sends fixed blocks at fixed intervals, 6 downloads each)

| offered | measured median | error |
|---|---|---|
| 1 Mbit/s | 0.99 Mbit/s | -1.40 % |
| 5 Mbit/s | 4.97 Mbit/s | -0.56 % |
| 20 Mbit/s | 20.10 Mbit/s | +0.51 % |
| 50 Mbit/s | 50.34 Mbit/s | +0.69 % |

Speed is counted from the arrival of the first data onwards. An earlier version also counted the first block, which arrives
together with the headers at no cost, and reported 3.53 Mbit/s for a server offering 3.2 Mbit/s; the tests now pin this.

### TCP connect on loopback

200 connections to an open port: 200 succeeded, median 0.022 ms, 99th percentile 0.134 ms.
A closed port is reported as a failure with the reason `refused`.

## 2. Does the analysis find the outages?

200 simulated months of one cycle a minute with three TCP targets. Each month has 3 to 10 injected outages
(median 5 minutes, up to 6 hours), 0 to 3 stretches in which the collector was not running, one server unreachable for two
hours while the connection was fine, one-cycle blips of total loss, 1 % ordinary packet loss, and in half of the long
outages the collector also stopped for a while in the middle of the outage.

| | result |
|---|---|
| injected outages (not touched by missing data) | 1110 |
| detected, each exactly once | 1110 (100.0 %) |
| false outages (one dead server, blips) | 0 |
| error of the reported start | between 0.0 s and 60.1 s (cycle length 60 s) |
| error of the reported duration | between -58.1 s and 60.2 s |

The resolution is one measurement cycle, as documented. Outages are never inflated by missing data:

| | outages that span a stretch with no data |
|---|---|
| ispq (72 outages were interrupted by the collector being off) | 0 (62 cut where the data stops) |
| the same data read naively, ignoring that cycles are missing | 62 |

### Availability

Error against the true share of the month the line was up, in percentage points:

| | mean error | worst |
|---|---|---|
| ispq (cycles with no data are left out) | 0.0086 | 0.0707 |
| naive (a missing cycle counts as down) | 0.5608 | 1.7643 |

### Why every sample is stored

Round-trip times drawn from a known distribution, 100000 samples, true 95th percentile 38.62 ms.

| | 95th percentile | error |
|---|---|---|
| from all the samples (what ispq stores) | 38.60 ms | -0.05 % |
| from per-round averages | 28.87 ms | -25.24 % |

Averaging five samples per round hides the tail, so a tool that stores only averages understates the slow cases by a quarter.

## 3. Cost

Synthetic data, one cycle a minute, three TCP targets and one DNS target.

| | |
|---|---|
| writing one cycle | 1.31 ms |
| database size | 16.3 MB per month (12 months: 195 MB, 2073600 rows) |
| report for one month (44640 cycles) | 1.81 s, plus 0.58 s to render the HTML |
| report over the whole 12 months (518400 cycles) | 23.48 s |

Timings depend on the machine and the disk; the size does not.
