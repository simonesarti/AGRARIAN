#!/usr/bin/env bash
# Portal authentication (/register, /login, /me) against a real db-writer and a
# real PostgreSQL, with TWO replicas.
#
# The second replica is the point, not padding: a session token is only worth
# having if it is stateless. One minted on replica 1 must be accepted by replica 2
# with no shared session store, which is exactly what an in-memory session would
# break — the same defect ws-server had with its in-memory client set.
#
# Usage:  ./run_portal_auth.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=portalauth-net
IMAGE=dbw-portalauthtest

cleanup() {
  docker rm -f pa-1 pa-2 pa-pg >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> building db-writer image"
docker build -q -t "$IMAGE" "$REPO/db_writer" >/dev/null

docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name pa-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec pa-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
ENVV=(-e DB_SERVICE=postgresql -e DB_HOST=pa-pg -e DB_PORT=5432 -e DB_NAME=testdb
      -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
      -e SESSION_JWT_SECRET="$SECRET")

echo "==> creating the schema (no seed user — registration makes its own)"
docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" \
  python rebuild_schema.py >/dev/null

echo "==> starting two replicas"
for n in 1 2; do
  docker run -d --name "pa-$n" --network "$NET" "${ENVV[@]}" "$IMAGE" >/dev/null
done

echo "==> waiting for both to answer /health"
for n in 1 2; do
  for _ in $(seq 1 30); do
    docker run --rm --network "$NET" curlimages/curl:latest \
      -sf "http://pa-$n:8000/health" >/dev/null 2>&1 && break
    sleep 1
  done
done

echo
docker run --rm --network "$NET" \
  -e DBW1=http://pa-1:8000 -e DBW2=http://pa-2:8000 \
  -v "$REPO/tests/comms:/tests:ro" -w /tests \
  python:3.12-slim python test_portal_auth.py
RC=$?

echo
echo "==> accounts actually in the database:"
docker exec pa-pg psql -U testuser -d testdb -c \
  "SELECT user_id, email FROM users ORDER BY user_id;"

echo "==> done (containers cleaned up)"
exit $RC
