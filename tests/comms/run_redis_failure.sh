#!/usr/bin/env bash
# Redis failure behaviour for ws-server: fast restart, then a sustained outage.
#
# Both tests need Redis restarted *while a viewer stays connected*, which the test
# container cannot do to itself — so the choreography lives here. Each test prints a
# marker when it is ready; this script acts on it.
#
# Usage:  ./run_redis_failure.sh          (from anywhere; uses the repo root)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=rrtest-net
SECRET_FILE=$(mktemp -d)/rr-secret.txt
IMAGE=wss-failuretest

cleanup() {
  docker rm -f rr-test rr-ws1 rr-ws2 rr-redis >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> building ws-server image"
docker build -q -t "$IMAGE" "$REPO/ws_server" >/dev/null

openssl rand -hex 32 > "$SECRET_FILE"
SECRET=$(cat "$SECRET_FILE")

docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name rr-redis --network "$NET" redis:7-alpine \
  redis-server --save "" --appendonly no >/dev/null
sleep 2

for n in 1 2; do
  docker run -d --name "rr-ws$n" --network "$NET" \
    -e WS_PORT=8765 -e REDIS_URL=redis://rr-redis:6379/0 \
    -e SESSION_JWT_SECRET="$SECRET" "$IMAGE" >/dev/null
done
sleep 4

run_test() {   # $1 = test file, $2.. = markers to act on
  local test_file=$1; shift
  docker rm -f rr-test >/dev/null 2>&1 || true
  docker run -d --name rr-test --network "$NET" \
    -v "$REPO/tests/comms:/tests:ro" -v "$(dirname "$SECRET_FILE"):/t:ro" -w /tests \
    python:3.11-slim sh -c \
    "pip install -q pyjwt websockets 2>&1|tail -0; python -u $test_file" >/dev/null

  for marker in "$@"; do
    for _ in $(seq 1 60); do
      docker logs rr-test 2>&1 | grep -q "$marker" && break
      sleep 1
    done
    case "$marker" in
      READY_FOR_RESTART) echo "    -> restarting redis"; docker restart rr-redis >/dev/null ;;
      STOP_REDIS)        echo "    -> stopping redis";   docker stop rr-redis >/dev/null ;;
      START_REDIS)       echo "    -> starting redis";   docker start rr-redis >/dev/null ;;
    esac
  done

  docker wait rr-test >/dev/null
  docker logs rr-test 2>&1 | grep -v '^\s*$'
}

echo
echo "==> test 1: fast restart"
run_test test_redis_reconnect.py READY_FOR_RESTART

echo
echo "==> test 2: sustained outage"
run_test test_redis_outage.py STOP_REDIS START_REDIS

echo
echo "==> done (containers cleaned up)"
