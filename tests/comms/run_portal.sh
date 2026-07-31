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
  docker rm -f pt-1 pt-2 pt-dbw pt-pg pt-redis >/dev/null 2>&1 || true
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

echo "==> starting redis (the rate limiters' shared counters)"
docker run -d --name pt-redis --network "$NET" \
  redis:7-alpine redis-server --save "" --appendonly no >/dev/null

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
#
# The two replicas differ in ONE setting: pt-1 trusts no proxy and pt-2 trusts
# one. That is not a shortcut for the test's convenience, it is the pair of
# configurations that need proving. pt-1 must ignore a forged X-Forwarded-For
# and keep counting against the peer address; pt-2 must believe the rightmost
# entry, which is what lets the test give each rate-limit scenario a clean
# bucket. Limits are lowered from their production defaults so exhausting one
# costs a handful of requests rather than thirty.
for n in 1 2; do
  hops=$(( n - 1 ))
  docker run -d --name "pt-$n" --network "$NET" \
    -e DB_WRITER_URL=http://pt-dbw:8000 \
    -e MEDIA_PUBLIC_HOST=media.test.local \
    -e WS_PUBLIC_HOST=ws.test.local \
    -e WS_PORT=8765 \
    -e REDIS_URL=redis://pt-redis:6379/1 \
    -e TRUSTED_PROXY_HOPS="$hops" \
    -e LOGIN_RATE_LIMIT_PER_ACCOUNT=5 \
    -e LOGIN_RATE_LIMIT_PER_IP=12 \
    -e REGISTER_RATE_LIMIT_PER_IP=8 \
    -e RATE_LIMIT_WINDOW_S=900 \
    -e REGISTER_RATE_WINDOW_S=900 \
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

# The rate limiter must fail OPEN. A limiter that turns a Redis outage into
# "nobody can sign in" has become a worse outage than the attack it prevents —
# so with Redis gone, both the check and the counting have to be skipped rather
# than raised. Asserted here rather than in the Python file because it needs to
# stop a container, and it is last because it does not put Redis back.
echo
echo "==> rate limiting fails open: stopping redis"
docker stop pt-redis >/dev/null

open_check() {
  if [ "$2" = "$3" ]; then
    echo "PASS  $1"
  else
    echo "FAIL  $1   [expected $2, got $3]"
    RC=1
  fi
}

post_login() {
  docker run --rm --network "$NET" curlimages/curl:latest \
    -s -o /dev/null -w '%{http_code}' -X POST -H "Origin: http://pt-1:8000" \
    --data-urlencode "email=alice@test.io" --data-urlencode "password=$1" \
    http://pt-1:8000/login
}

open_check "a correct sign-in still works with the limiter's store down" \
  303 "$(post_login 'correct horse')"
open_check "a wrong password is still a 401, not a 500" \
  401 "$(post_login 'wrong')"

echo
echo "==> what the portal actually wrote:"
docker exec pt-pg psql -U testuser -d testdb -c \
  "SELECT u.email, s.stream_id, s.label, s.revoked_at IS NULL AS active
     FROM users u LEFT JOIN streams s ON s.user_id = u.user_id
    ORDER BY u.user_id, s.stream_id;"

echo "==> done (containers cleaned up)"
exit $RC
