#!/usr/bin/env bash
# End to end inside the image: the collector runs (read-only root, no capabilities) against two local servers,
# then reports are produced from the same volume. Needs Docker. Usage: tools/docker_e2e.sh [image]
set -euo pipefail
IMAGE="${1:-ispq}"
VOL="ispq-e2e-$$"
NET="ispq-e2e-net-$$"
SRV="ispq-e2e-srv-$$"
trap 'docker rm -f "$SRV" >/dev/null 2>&1 || true; docker volume rm -f "$VOL" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true' EXIT

[ "$(docker run --rm --entrypoint id "$IMAGE" -u)" != 0 ]   # not root
docker network create "$NET" >/dev/null

# a server inside the same network that the collector can measure: a web server answers on port 8000
docker run -d --name "$SRV" --network "$NET" --entrypoint python "$IMAGE" -m http.server 8000 >/dev/null
sleep 2

CONF="$(mktemp)"
chmod 644 "$CONF"   # mktemp makes it 0600; the container user is not the runner user
cat > "$CONF" <<TOML
interval = 5
database = "/data/ispq.db"
[[tcp]]
name = "web"
host = "$SRV"
port = 8000
count = 3
gap = 0.05
[[http]]
name = "page"
url = "http://$SRV:8000/"
bandwidth = true
TOML
docker volume create "$VOL" >/dev/null
docker run --rm -v "$VOL":/data -v "$CONF":/in.toml:ro --entrypoint sh "$IMAGE" -c 'cat /in.toml > /data/ispq.toml'

RUN=(docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp --network "$NET" -v "$VOL":/data "$IMAGE")
"${RUN[@]}" run --config /data/ispq.toml --cycles 3 | tee /dev/stderr | grep -q 'web 3/3'
"${RUN[@]}" status --config /data/ispq.toml | tee /dev/stderr | grep -q 'cycles     3'
"${RUN[@]}" report --config /data/ispq.toml --format json | tee /dev/stderr | grep -q '"cycles": 3'
"${RUN[@]}" report --config /data/ispq.toml --format html | grep -q '<!doctype html>'
"${RUN[@]}" export-influx --config /data/ispq.toml | grep -q '^ispq,kind=tcp,target=web '
# a measurement of something that is not there is a failure, with exit code 1
docker rm -f "$SRV" >/dev/null
if "${RUN[@]}" once --config /data/ispq.toml --no-save >/dev/null; then echo "expected the down state to give exit 1" >&2; exit 1; fi
echo "docker end-to-end: OK"
