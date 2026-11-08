# Limitations

- **It measures from where it runs**, to the targets you list. A problem at the provider, in your router or in Wi-Fi all look
  the same; run it on a wired machine to leave Wi-Fi out.
- **TCP connect time is not ping.** It includes the target's accept queue; it is a good measure of what applications feel,
  not of the raw path. Loss is "an attempt that did not complete", not packet loss in the strict sense.
- **Outage resolution is one cycle** (60 s by default): start and length are accurate to within one interval, and an
  outage shorter than two cycles is a blip.
- **Captive portals** that answer every HTTP request with a login page look "up" to the HTTP probe; the TCP probe is the
  reference for availability.
- **Time zones:** months and days are in UTC, so a report for "November" starts at 01:00 or 02:00 local time in Italy.
- **Download tests use data.** They are off by default; a 5 MB download every hour is about 3.6 GB a month. Use `every` and `max_bytes`.
- **Not tested against a real InfluxDB or Grafana:** the export is checked for the line-protocol format only.
- **The evaluation is on loopback and on simulated data** (see docs/evaluation.md); it does not rank providers.
- One writer per database file.
