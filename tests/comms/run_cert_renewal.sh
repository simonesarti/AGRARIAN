#!/usr/bin/env bash
# What happens to a running terminator when its certificate is replaced on disk.
#
# WHY THIS RUNNER EXISTS
# ----------------------
# §9 carried this as an open item with a deadline attached: the answer is needed
# BEFORE the first real certificate is issued, not before it expires. Three
# services now read a leaf from disk (§7), cert-manager will replace that leaf
# every sixty days or so, and if a service does not reread it then renewal means
# a restart — which for MediaMTX drops every flight in the air.
#
# The answers turned out to be the opposite of what §7 assumed, which is the
# reason this is a runner and not a paragraph:
#
#   MediaMTX   rereads by itself, per connection, within seconds. No restart, and
#              a publish already in the air is not disturbed.
#   Mosquitto  does not, until SIGHUP — as documented, now confirmed.
#   Traefik    does NOT, despite `watch: true`. Its file provider watches the
#              DIRECTORY named by `providers.file.directory`, and the certificate
#              lives outside it at /etc/traefik/certs, so nothing fires. Touching
#              any file in the watched directory reloads the configuration and the
#              certificate with it.
#
# Each of those is a property of somebody else's binary, so each is exactly the
# kind of thing that changes under an upgrade without anybody noticing until a
# certificate expires. That is what this file is for.
#
# Host ports 51936/58322/58883/50443 are used so this cannot collide with a
# running compose stack.
#
# Usage:  ./run_cert_renewal.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=certrenewal-net
IMAGE=dbw-certrenewal
CERTS="$(mktemp -d)"
# A writable copy of the dynamic directory: one assertion has to touch a file in
# it, and the repo's own copy is mounted read-only everywhere else for a reason.
DYN="$(mktemp -d)"

cleanup() {
  docker rm -f cr-mediamtx cr-mosquitto cr-traefik db-writer cr-pg >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
  kill "${FFPID:-}" >/dev/null 2>&1 || true
  rm -rf "$CERTS" "$DYN"
}
trap cleanup EXIT

pass=0; fail=0
check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "PASS  $1   [$3]"; pass=$((pass+1))
  else echo "FAIL  $1   [expected $2, got $3]"; fail=$((fail+1)); fi
}

for tool in ffmpeg curl python3 openssl; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool"; exit 1; }
done

serial_of_file() { openssl x509 -in "$1" -noout -serial | cut -d= -f2; }

# The serial the LISTENER is serving, on a NEW connection. New matters: an
# established session keeps whatever it negotiated, so reusing one would report a
# stale answer as a definitive one.
served() {  # served <port> -> old | new | <unexpected serial>
  local s
  s=$(echo Q | openssl s_client -connect "127.0.0.1:$1" -CAfile "$CERTS/ca/ca.crt" 2>/dev/null \
      | openssl x509 -noout -serial 2>/dev/null | cut -d= -f2)
  case "$s" in
    "$S1") echo old ;;
    "$S2") echo new ;;
    "")    echo no-answer ;;
    *)     echo "$s" ;;
  esac
}

echo "==> issuing the first leaf"
CERT_DIR="$CERTS" "$REPO/scripts/generate_local_certs.sh" agrarian.local >/dev/null
S1=$(serial_of_file "$CERTS/server/server.crt")
S2=""   # not yet issued; served() reports anything unknown as its own serial
cp "$REPO/configs/traefik/dynamic/"*.yml "$DYN/"

echo "==> building db-writer image"
docker build -q -t "$IMAGE" "$REPO/db_writer" >/dev/null || exit 1

docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name cr-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null
echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec cr-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
ENVV=(-e DB_SERVICE=postgresql -e DB_HOST=cr-pg -e DB_PORT=5432 -e DB_NAME=testdb
      -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
      -e SESSION_JWT_SECRET="$SECRET")

SEED=$(docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" \
  sh -c "echo yes | python rebuild_schema.py --drop --seed-user a@t.io --seed-password pw12345678" 2>&1)
KEY=$(echo "$SEED" | sed -n 's/.*stream key : \([a-z0-9]*\).*/\1/p')
[ -n "$KEY" ] || { echo "seeding failed"; exit 1; }

echo "==> starting db-writer and the three terminators against that leaf"
docker run -d --name db-writer --network "$NET" "${ENVV[@]}" "$IMAGE" >/dev/null
docker run -d --name cr-mediamtx --network "$NET" -p 51936:1936 -p 58322:8322 \
  -v "$REPO/configs/mediamtx/mediamtx.yaml:/mediamtx.yml:ro" \
  -v "$CERTS/server:/certs:ro" \
  bluenviron/mediamtx:latest-ffmpeg >/dev/null
docker run -d --name cr-mosquitto --network "$NET" -p 58883:8883 \
  -v "$REPO/configs/mosquitto/mosquitto.conf:/etc/mosquitto/mosquitto.conf:ro" \
  -v "$CERTS/server:/mosquitto/certs:ro" \
  iegomez/mosquitto-go-auth:2.1.0-mosquitto_2.0.15 >/dev/null
docker run -d --name cr-traefik --network "$NET" -p 50443:443 \
  -v "$REPO/configs/traefik/traefik.yml:/etc/traefik/traefik.yml:ro" \
  -v "$DYN:/etc/traefik/dynamic" \
  -v "$CERTS/server:/etc/traefik/certs:ro" \
  traefik:v3.3 >/dev/null
sleep 8

echo
echo "── everything starts on the first leaf ─────────────────────────────────────"
check "MediaMTX serves the first leaf on RTMPS" old "$(served 51936)"
check "MediaMTX serves the first leaf on RTSPS" old "$(served 58322)"
check "Mosquitto serves the first leaf on MQTTS" old "$(served 58883)"
check "Traefik serves the first leaf on 443" old "$(served 50443)"

# An AUTHORISED publish, in the air across the renewal. Authorised matters: an
# earlier version of this ran without db-writer, so the publish was refused at the
# auth hook and "the publisher survived" was a statement about ffmpeg retrying.
echo
echo "== taking off, so the renewal happens under a live flight =="
ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=15 \
  -t 300 -c:v libx264 -preset ultrafast -tune zerolatency -f flv \
  -tls_verify 1 -ca_file "$CERTS/ca/ca.crt" \
  "rtmps://127.0.0.1:51936/in/$KEY" >/dev/null 2>&1 &
FFPID=$!
sleep 6
check "the publish was actually authorised (everything below depends on it)" 1 \
      "$(docker logs cr-mediamtx 2>&1 | grep -c "is publishing to path 'in/$KEY'")"

echo
echo "── renew the leaf under the running services ───────────────────────────────"
CERT_DIR="$CERTS" "$REPO/scripts/generate_local_certs.sh" --renew-leaf agrarian.local >/dev/null
S2=$(serial_of_file "$CERTS/server/server.crt")
# The control. If --renew-leaf reissued the same serial, every assertion after
# this one would pass while measuring nothing at all.
check "the renewal really issued a different certificate" different \
      "$([ "$S1" != "$S2" ] && echo different || echo identical)"
sleep 10

echo
echo "── who noticed, unaided ────────────────────────────────────────────────────"
# The finding that closes §9's open item, and it is the good direction: the one
# service whose restart would drop every flight in the air is the one that needs
# no restart.
check "MediaMTX picked up the new leaf by itself, on RTMPS" new "$(served 51936)"
check "and on RTSPS" new "$(served 58322)"
check "the flight already in the air was NOT disturbed by it" alive \
      "$(kill -0 $FFPID 2>/dev/null && echo alive || echo dropped)"
check "MediaMTX still has the path published" 1 \
      "$(docker logs cr-mediamtx 2>&1 | grep -c "is publishing to path 'in/$KEY'")"

# Documented in §7 and true.
check "Mosquitto did NOT — it still serves the old leaf" old "$(served 58883)"

# NOT documented in §7, which claimed the opposite. Traefik's file provider
# watches providers.file.directory; the certificate is mounted outside it, so no
# reload event ever fires.
check "Traefik did NOT either, despite watch: true" old "$(served 50443)"

echo
echo "── what does make each of them notice ──────────────────────────────────────"
docker kill -s HUP cr-mosquitto >/dev/null 2>&1
sleep 4
check "Mosquitto rereads its certificate on SIGHUP" new "$(served 58883)"

# Touch, not edit: the config is unchanged and only its mtime moves, which is
# what a renewal hook would do rather than rewriting routing rules.
touch "$DYN/routers.yml"
sleep 6
check "Traefik rereads it when a file in the watched directory is touched" new \
      "$(served 50443)"

echo
echo "── and what must never be done ─────────────────────────────────────────────"
# Recorded as an assertion so that nobody reaches for the obvious symmetry with
# Mosquitto and adds a SIGHUP to a renewal hook. It does not reload MediaMTX; it
# ends it, and with it every flight in the air.
docker kill -s HUP cr-mediamtx >/dev/null 2>&1
sleep 4
check "SIGHUP KILLS MediaMTX rather than reloading it" false \
      "$(docker inspect -f '{{.State.Running}}' cr-mediamtx 2>/dev/null)"

kill $FFPID >/dev/null 2>&1; wait $FFPID 2>/dev/null

echo
echo "=========================================================="
echo "old leaf: $S1"
echo "new leaf: $S2"
echo "$pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ] || { echo; echo "--- mediamtx ---"; docker logs cr-mediamtx 2>&1 | tail -15
                       echo; echo "--- traefik ---"; docker logs cr-traefik 2>&1 | tail -15; exit 1; }
