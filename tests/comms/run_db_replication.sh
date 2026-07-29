#!/usr/bin/env bash
# db-writer replica safety, against a real PostgreSQL.
#
# SQLite will not do here: the bug this guards against was a flight opened on one
# replica being unwritable on another, which only appears with two live processes
# sharing a database.
#
# Usage:  ./run_db_replication.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=dbwtest-net
IMAGE=dbw-replicationtest

cleanup() {
  docker rm -f dbw-test dbw-1 dbw-2 dbw-pg >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> building db-writer image"
docker build -q -t "$IMAGE" "$REPO/db_writer" >/dev/null

docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name dbw-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec dbw-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
ENVV=(-e DB_SERVICE=postgresql -e DB_HOST=dbw-pg -e DB_PORT=5432 -e DB_NAME=testdb
      -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
      -e SESSION_JWT_SECRET="$SECRET")

echo "==> seeding schema, one user, one stream"
docker run --rm --network "$NET" "${ENVV[@]}" "$IMAGE" \
  sh -c "echo yes | python rebuild_schema.py --drop --seed-user pilot@test.io --seed-password pw123" \
  2>&1 | tail -5

echo "==> starting two replicas"
for n in 1 2; do
  docker run -d --name "dbw-$n" --network "$NET" "${ENVV[@]}" "$IMAGE" >/dev/null
done
sleep 5

run_test() {
  docker rm -f dbw-test >/dev/null 2>&1 || true
  docker run --rm --network "$NET" -v "$REPO/tests/comms:/tests:ro" -w /tests \
    python:3.11-slim python "$1"
}

echo
echo "==> test 1: a flight opened on replica 1 is writable on replica 2"
run_test test_replication.py

echo
echo "==> test 2: concurrent flights interleaved across both replicas"
run_test test_multiflight.py

echo
echo "==> rows actually in the database:"
docker exec dbw-pg psql -U testuser -d testdb -c \
  "SELECT flight_id, COUNT(*) AS alerts FROM alerts GROUP BY flight_id ORDER BY flight_id;"

echo "==> done (containers cleaned up)"
