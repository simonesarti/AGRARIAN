#!/usr/bin/env bash
# End-to-end Mosquitto authorisation, against a real mosquitto-go-auth broker,
# db-writer and Postgres.
#
# test_mqtt_auth.py checks the decision in isolation. This checks the thing that
# file cannot: that Mosquitto is actually consulting db-writer and obeying it, for
# real CONNECTs, PUBLISHes and SUBSCRIBEs over the wire.
#
# Two tenants exist throughout, so "denied" is proved against a *valid credential
# belonging to somebody else* rather than only against no credential at all.
#
# Three different signals are needed, because MQTT does not give one uniform
# "denied" response:
#   - CONNECT refused (bad/revoked identity)   -> mosquitto_pub/sub exits 5
#   - SUBSCRIBE denied (acc=4, wrong topic)    -> "All subscription requests
#     were denied." on stdout; the exit code is 0 either way, so it is useless
#     here — the same trap as ffmpeg's exit code against MediaMTX.
#   - PUBLISH denied (acc=2, QoS 0)            -> no client-side signal at all;
#     the broker silently drops it. The authoritative signal is Mosquitto's own
#     log line ("error code: 401"), counted before/after like run_mediamtx_auth.sh
#     counts MediaMTX's "is publishing to path" line.
#
# Host ports 21883/28002 are used so this cannot collide with a running compose
# stack. Needs mosquitto_pub/mosquitto_sub on the host... no: run via a
# throwaway eclipse-mosquitto container instead, so only curl/openssl/python3
# and Docker are required on the host.
#
# Usage:  ./run_mqtt_auth.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Mosquitto terminates its own TLS now (§7), and it fails to start the 8883
# listener if the certificate mosquitto.conf points at is not on disk. So every
# runner that mounts the real mosquitto.conf has to supply one, whether or not it
# tests TLS. Issued into a throwaway directory rather than into certificates/,
# whose CA may already be installed in somebody's browser.
CERTS="$(mktemp -d)"
CERT_DIR="$CERTS" "$REPO/scripts/generate_local_certs.sh" >/dev/null \
  || { echo "could not issue a local certificate for Mosquitto"; exit 1; }
NET=mqttauth-net
IMAGE=dbw-mqttauthtest
MOSQ_IMAGE=iegomez/mosquitto-go-auth:2.1.0-mosquitto_2.0.15
API=http://localhost:28002

cleanup() {
  rm -rf "$CERTS"
  docker rm -f mqtta-mosquitto db-writer mqtta-pg >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

pass=0; fail=0
check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "PASS  $1   [$3]"; pass=$((pass+1))
  else echo "FAIL  $1   [expected $2, got $3]"; fail=$((fail+1)); fi
}

for tool in curl python3 openssl; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool"; exit 1; }
done

echo "==> building db-writer image"
docker build -q -t "$IMAGE" "$REPO/db_writer" >/dev/null || exit 1

docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name mqtta-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec mqtta-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
ENVV=(-e DB_SERVICE=postgresql -e DB_HOST=mqtta-pg -e DB_PORT=5432 -e DB_NAME=testdb
      -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
      -e SESSION_JWT_SECRET="$SECRET")

echo "==> seeding two tenants, one stream each"
SEED=$(docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" \
  sh -c "echo yes | python rebuild_schema.py --drop --seed-user alice@test.io --seed-password pw12345678" 2>&1)
KEY_A=$(echo "$SEED" | sed -n 's/.*stream key : \([a-z0-9]*\).*/\1/p')

KEY_B=$(docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" python -c "
from db_manager import UserDirectory, User
from sqlalchemy.orm import sessionmaker
d = UserDirectory('postgresql://testuser:testpw@mqtta-pg:5432/testdb')
S = sessionmaker(bind=d._engine)
with S() as s:
    u = User(email='bob@test.io', password=User.hash_password('pw456'))
    s.add(u); s.commit()
    uid = u.user_id
print(d.create_stream(uid, 'bob stream')['stream_key'])
" 2>/dev/null | tail -1)

echo "    alice key=$KEY_A   bob key=$KEY_B"
[ -n "$KEY_A" ] && [ -n "$KEY_B" ] || { echo "seeding failed"; exit 1; }

echo "==> starting db-writer and mosquitto (mosquitto-go-auth)"
# db-writer MUST be named exactly 'db-writer': mosquitto.conf hardcodes
# auth_opt_http_host db-writer, same convention run_mediamtx_auth.sh follows for
# mediamtx.yaml's hardcoded hook hostnames.
docker run -d --name db-writer --network "$NET" -p 28002:8000 "${ENVV[@]}" "$IMAGE" >/dev/null \
  || { echo "db-writer failed to start"; exit 1; }
docker run -d --name mqtta-mosquitto --network "$NET" -p 21883:1883 \
  -v "$REPO/configs/mosquitto/mosquitto.conf:/etc/mosquitto/mosquitto.conf:ro" \
  -v "$CERTS/server:/mosquitto/certs:ro" \
  "$MOSQ_IMAGE" >/dev/null \
  || { echo "mosquitto failed to start (is a previous run still holding 21883?)"; exit 1; }
sleep 4

for c in db-writer mqtta-mosquitto; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] \
    || { echo "$c exited during startup:"; docker logs "$c" 2>&1 | tail -20; exit 1; }
done

jsonget() { python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }

open_flight() {  # open_flight <stream_key>  -- what the orchestrator calls
  curl -s -X POST "$API/flight/open" -H 'Content-Type: application/json' \
    -d "{\"stream_key\":\"$1\"}"
}

A=$(open_flight "$KEY_A")
B=$(open_flight "$KEY_B")
PUB_A=$(echo "$A" | jsonget publisher_token)
PUB_B=$(echo "$B" | jsonget publisher_token)
[ -n "$PUB_A" ] && [ -n "$PUB_B" ] || { echo "could not open flights: $A / $B"; exit 1; }
echo "    alice flight opened, bob flight opened"

mosq() {  # mosq <sub|pub> <user> <pass> <topic> [extra mosquitto_pub/sub args...]
  local cmd="$1" user="$2" pw="$3" topic="$4"; shift 4
  docker run --rm --network "$NET" eclipse-mosquitto:2 \
    "mosquitto_$cmd" -h mqtta-mosquitto -p 1883 -u "$user" -P "$pw" -t "$topic" "$@" 2>&1
}

denied_count() { docker logs mqtta-mosquitto 2>&1 | grep -c 'error code: 401'; }

echo
echo "── CONNECT: identity check ─────────────────────────────────────────────────"
out=$(mosq pub "$KEY_A" x "telemetry/$KEY_A/latitude" -m 1); rc=$?
check "drone connects with a live stream key" 0 "$rc"
out=$(mosq pub zzzzzzzzzzzzzzzz x "telemetry/zzzzzzzzzzzzzzzz/latitude" -m 1); rc=$?
check "unknown/revoked stream key refused at CONNECT" 5 "$rc"
out=$(mosq pub not-a-jwt-at-all x "telemetry/$KEY_A/latitude" -m 1); rc=$?
check "garbage credential refused at CONNECT" 5 "$rc"

echo
echo "── SUBSCRIBE (acc=4): the app container ─────────────────────────────────────"
out=$(mosq sub "$PUB_A" x "telemetry/$KEY_A/latitude" -C 1 -W 2 -d)
check "app subscribes to the telemetry of its own flight's stream" 0 \
      "$(echo "$out" | grep -c 'All subscription requests were denied')"
out=$(mosq sub "$PUB_A" x "telemetry/$KEY_B/latitude" -C 1 -W 2 -d)
check "app CANNOT subscribe to another tenant's telemetry" 1 \
      "$(echo "$out" | grep -c 'All subscription requests were denied')"
out=$(mosq sub "$PUB_B" x "telemetry/$KEY_A/latitude" -C 1 -W 2 -d)
check "bob's token cannot subscribe to alice's telemetry either" 1 \
      "$(echo "$out" | grep -c 'All subscription requests were denied')"

echo
echo "── PUBLISH (acc=2, QoS 0 — no client-side signal, check the broker's log) ──"
before=$(denied_count)
mosq pub "$KEY_A" x "telemetry/$KEY_A/latitude" -m 44.0 >/dev/null
check "drone publishing under its own key is NOT denied" "$before" "$(denied_count)"

before=$(denied_count)
mosq pub "$KEY_A" x "telemetry/$KEY_B/latitude" -m 44.0 >/dev/null
after=$(denied_count)
check "drone CANNOT publish under another tenant's key" 1 "$((after-before))"

echo
echo "── a subscribed app really receives its own drone's telemetry ─────────────"
out=$(mosq sub "$PUB_A" x "telemetry/$KEY_A/rel_alt" -C 1 -W 4 &
      sleep 1; mosq pub "$KEY_A" x "telemetry/$KEY_A/rel_alt" -m 12.5 >/dev/null; wait)
check "the value published by the drone is the value the app receives" 12.5 \
      "$(echo "$out" | tail -1)"

echo
echo "=========================================================="
echo "$pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ] || { echo; echo "--- mosquitto log ---"; docker logs mqtta-mosquitto 2>&1 | tail -30; exit 1; }
