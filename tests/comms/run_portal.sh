#!/usr/bin/env bash
# The portal, driven the way a browser drives it: form posts, a session cookie,
# and an Origin header — against a real db-writer and a real PostgreSQL.
#
# TWO portal replicas, for the same reason run_portal_auth.sh runs two db-writers:
# the session lives in a signed cookie and nowhere else, so a cookie issued by
# replica 1 must be accepted by replica 2 with no shared store between them. A
# server-side session would pass every other assertion in this file and fail that
# one — which is exactly the defect ws-server once had with its in-memory client
# set.
#
# The portal is started with its PRODUCTION defaults (COOKIE_SECURE=true,
# PUBLIC_TLS=true) even though the test speaks plain HTTP. A browser would refuse
# to return a Secure cookie over http://; this client does not, which is what lets
# the cookie attributes themselves be asserted rather than quietly relaxed.
#
# Usage:  ./run_portal.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=portal-net
DBW_IMAGE=dbw-portaltest
PORTAL_IMAGE=portal-portaltest

cleanup() {
  docker rm -f pt-1 pt-2 pt-dbw pt-pg >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$PORTAL_IMAGE" "$DBW_IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> building images"
docker build -q -t "$DBW_IMAGE" "$REPO/db_writer" >/dev/null
docker build -q -t "$PORTAL_IMAGE" "$REPO/portal" >/dev/null

docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name pt-pg --network "$NET" \
  -e POSTGRES_PASSWORD=testpw -e POSTGRES_USER=testuser -e POSTGRES_DB=testdb \
  postgres:16-alpine >/dev/null

echo "==> waiting for postgres"
for _ in $(seq 1 30); do
  docker exec pt-pg pg_isready -U testuser >/dev/null 2>&1 && break
  sleep 1
done

SECRET=$(openssl rand -hex 32)
DBENV=(-e DB_SERVICE=postgresql -e DB_HOST=pt-pg -e DB_PORT=5432 -e DB_NAME=testdb
       -e DB_WORKER_NAME=testuser -e DB_WORKER_PASSWORD=testpw
       -e SESSION_JWT_SECRET="$SECRET")

echo "==> creating the schema (no seed — the portal registers its own accounts)"
docker run --rm --network "$NET" "${DBENV[@]}" "$DBW_IMAGE" \
  python rebuild_schema.py >/dev/null

echo "==> starting db-writer"
docker run -d --name pt-dbw --network "$NET" "${DBENV[@]}" "$DBW_IMAGE" >/dev/null

echo "==> starting two portal replicas"
# No SESSION_JWT_SECRET and no DB_* here, deliberately: if the portal can still
# serve every page in this test without them, it is not validating or reading
# anything itself — which is the property §7 claims.
for n in 1 2; do
  docker run -d --name "pt-$n" --network "$NET" \
    -e DB_WRITER_URL=http://pt-dbw:8000 \
    -e MEDIA_PUBLIC_HOST=media.test.local \
    -e WS_PUBLIC_HOST=ws.test.local \
    -e WS_PORT=8765 \
    "$PORTAL_IMAGE" >/dev/null
done

echo "==> waiting for /health on db-writer and both replicas"
for h in pt-dbw pt-1 pt-2; do
  for _ in $(seq 1 30); do
    docker run --rm --network "$NET" curlimages/curl:latest \
      -sf "http://$h:8000/health" >/dev/null 2>&1 && break
    sleep 1
  done
done

echo
docker run --rm --network "$NET" \
  -e PORTAL1=http://pt-1:8000 -e PORTAL2=http://pt-2:8000 \
  -e DBW=http://pt-dbw:8000 \
  -v "$REPO/tests/comms:/tests:ro" -w /tests \
  python:3.12-slim python test_portal.py
RC=$?

echo
echo "==> what the portal actually wrote:"
docker exec pt-pg psql -U testuser -d testdb -c \
  "SELECT u.email, s.stream_id, s.label, s.revoked_at IS NULL AS active
     FROM users u LEFT JOIN streams s ON s.user_id = u.user_id
    ORDER BY u.user_id, s.stream_id;"

echo "==> done (containers cleaned up)"
exit $RC
