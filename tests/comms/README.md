# Communication hub tests

These are the tests behind every "verified" claim in `CLOUD_ARCHITECTURE.md` §9. They
cover the multi-tenancy, authentication and replication work on the `secure-cloud`
branch — the parts where a regression is silent rather than loud.

Everything runs in throwaway containers. **The host interpreter has none of the
dependencies** (`sqlalchemy`, `bcrypt`, `pyjwt`, `websockets` are all absent), so do not
try to run these directly with `python3`.

---

## Quick reference

| Test | Needs | How to run |
| --- | --- | --- |
| `test_schema.py` | nothing | one-liner below |
| `test_tokens.py` | nothing | one-liner below |
| `test_mediamtx_auth.py` | nothing | one-liner below |
| `test_replication.py` | Postgres + 2 db-writer replicas | `./run_db_replication.sh` |
| `test_multiflight.py` | same | `./run_db_replication.sh` |
| `test_redis_reconnect.py` | Redis + 2 ws-server replicas | `./run_redis_failure.sh` |
| `test_redis_outage.py` | same | `./run_redis_failure.sh` |
| `run_mediamtx_auth.sh` | MediaMTX + db-writer + Postgres, host `ffmpeg` | `./run_mediamtx_auth.sh` |
| `test_tenancy.py` | running ws-server + Redis | see below |
| `test_replicas.py` | 2 ws-server replicas + Redis | see below |

The two `run_*.sh` scripts build images, stand everything up, run the tests, print the
resulting database rows where relevant, and **clean up after themselves on exit** —
including on failure.

---

## No stack required

The MediaMTX authorisation decision, with no database and no media server:

```bash
docker run --rm -v "$PWD/db_writer:/w" -v "$PWD/tests/comms:/tests:ro" \
  -w /tmp -e DB_WRITER_DIR=/w python:3.11-slim \
  sh -c "pip install -q pyjwt; python /tests/test_mediamtx_auth.py"
```

Schema behaviour and stream management, against SQLite in memory — the fastest signal
that the data model is intact:

```bash
docker run --rm -v "$PWD/db_writer:/w" -v "$PWD/tests/comms:/tests:ro" \
  -w /tmp -e DB_WRITER_DIR=/w python:3.11-slim \
  sh -c "pip install -q sqlalchemy bcrypt; python /tests/test_schema.py"
```

Token scope separation and cross-flight replay:

```bash
docker run --rm -v "$PWD/ws_server:/ws" -v "$PWD/tests/comms:/tests:ro" \
  -w /tmp -e WS_SERVER_DIR=/ws python:3.11-slim \
  sh -c "pip install -q pyjwt; python /tests/test_tokens.py"
```

`test_tokens.py` prints an `InsecureKeyLengthWarning` — that is the *deliberately forged*
token signed with a 17-character wrong secret, not a real key. Expected.

## Orchestrated

```bash
./run_db_replication.sh      # 7 + 20×2 assertions, real PostgreSQL
./run_redis_failure.sh       # 3 + 7 assertions, Redis restarted mid-test
./run_mediamtx_auth.sh       # 16 assertions, real MediaMTX + ffmpeg publishes
```

`run_mediamtx_auth.sh` needs `ffmpeg` and `curl` **on the host** and binds host ports
11935/18888/18002 so it cannot collide with a running compose stack. If a previous run
left containers behind it aborts rather than proceeding — see the note below.

## Against a running compose stack

`test_tenancy.py` and `test_replicas.py` talk to a live ws-server. Bring the stack up
(`docker compose up -d ws-server redis`), then:

```bash
docker run --rm --network comms-net -v "$PWD/tests/comms:/tests:ro" -w /tests \
  -e SESSION_JWT_SECRET="$SESSION_JWT_SECRET" python:3.11-slim \
  sh -c "pip install -q pyjwt websockets; python test_tenancy.py"
```

`test_replicas.py` additionally needs a second replica on the same network and Redis:

```bash
docker run -d --name ws-replica-2 --network comms-net \
  -e WS_PORT=8765 -e REDIS_URL=redis://redis:6379/0 \
  -e SESSION_JWT_SECRET="$SESSION_JWT_SECRET" agrarian-comms-ws-server
```

Override `WS_URL` / `API_URL` (tenancy) or `PUBLISH_TO` / `VIEW_ON` (replicas) if your
hostnames differ. Remove `ws-replica-2` when finished — it is not part of the compose
stack.

---

## What each test actually guards

**`test_schema.py`** — the three portal operations, pinned: *add* mints a unique key,
*rotate* changes the key while touching no flight or alert, *remove* revokes and hides
while deleting nothing. Plus: `flights` carries no `user_id` (ownership is reached
through `streams`, so nothing can contradict it), cross-user access is refused, a flight
without a `stream_id` is rejected, and deleting a *user* still cascades — the one case
that cascade exists for. Also asserts `delete_stream()` does **not** exist; a portal
"Remove" button implemented as a hard delete would silently destroy flight history.

**`test_tokens.py`** — viewer and publisher tokens are signed with the same secret, so
the `scope` claim is the only thing separating them. Checked in both directions, plus
cross-flight replay, forged signatures, expiry, and a scopeless token (which must fail
rather than bypass the check).

**`test_replication.py` / `test_multiflight.py`** — a flight opened on replica 1 must be
writable on replica 2. This guards the bug where db-writer kept a per-flight manager in
process memory and 404'd on every replica but the one that opened the flight. Note the
tests confirm **rows in the database**, not just HTTP 200 — accepting a write is not the
same as persisting it.

**`test_redis_reconnect.py` / `test_redis_outage.py`** — a viewer stays connected while
Redis dies. If redis-py did not resubscribe on reconnect, the socket would stay open and
silently never deliver again. Also pins the behaviour that makes a single Redis instance
tolerable: publishes fail **loudly** (HTTP 500) during an outage rather than returning
200 and dropping the alert.

**`test_mediamtx_auth.py`** — the four legitimate action/path combinations and nothing
else, checked against a stub directory with two tenants so every denial is proved
against a *valid credential belonging to someone else*, not merely against no
credential. Includes the anchoring cases (prefix/suffix smuggling, uppercase keys) and
both directions of scope confusion, which is the only thing separating a viewer token
from a publisher token for the same flight.

**`run_mediamtx_auth.sh`** — the part the file above cannot check: that MediaMTX
actually consults the endpoint and obeys it, over real RTMP publishes and real HLS
reads. Two traps are baked in, both of which produced convincing false results first
time round:

- **`ffmpeg`'s exit code is meaningless here.** It returns 0 whether MediaMTX accepted
  the stream or rejected it at authentication — the FLV muxer only reports that it
  could not rewrite a non-seekable header. The assertion is on MediaMTX's own
  `is publishing to path '<x>'` log line instead.
- **MediaMTX's HLS server 302s to `?cookieCheck=1`** and authenticates only the
  followed request. Without `curl -L` and a cookie jar every result is 302 and the
  test silently measures nothing.

It also aborts if MediaMTX fails to start, because a MediaMTX that never came up
denies everything — which reads as a wall of passing deny-assertions.

**`test_tenancy.py`** — the original leak. ws-server used to broadcast every alert,
including its JPEG and GPS position, to every connected client. Assertion 6 is the one
that matters: flight 2's viewer must receive **nothing**.

---

## Gaps

There is no coverage for Mosquitto ACLs, the orchestrator, or TLS — none of those exist
yet. See `CLOUD_ARCHITECTURE.md` §9 for current status.

MediaMTX auth is covered, but only for the paths the *hub* serves. Nothing yet checks
the app tier against them: the app still publishes to the retired `annot` path and
reads `drone`, and is rewired in the orchestrator step.
