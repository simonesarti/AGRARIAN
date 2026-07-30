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
| `test_mqtt_auth.py` | nothing | one-liner below |
| `test_orchestrator.py` | nothing | one-liner below |
| `test_replication.py` | Postgres + 2 db-writer replicas | `./run_db_replication.sh` |
| `test_multiflight.py` | same | `./run_db_replication.sh` |
| `test_redis_reconnect.py` | Redis + 2 ws-server replicas | `./run_redis_failure.sh` |
| `test_redis_outage.py` | same | `./run_redis_failure.sh` |
| `run_mediamtx_auth.sh` | MediaMTX + db-writer + Postgres, host `ffmpeg` | `./run_mediamtx_auth.sh` |
| `run_orchestrator.sh` | the above + Docker socket, host `ffmpeg` | `./run_orchestrator.sh` |
| `run_orchestrator_real_app.sh` | the above + a GPU, `nvidia-container-toolkit`, `checkpoints/*.pt` | `./run_orchestrator_real_app.sh` |
| `run_recording_upload.sh` | MediaMTX + recorder + db-writer + Postgres, host `ffmpeg` | `./run_recording_upload.sh` |
| `run_mqtt_auth.sh` | mosquitto-go-auth + db-writer + Postgres | `./run_mqtt_auth.sh` |
| `run_orchestrator_recovery.sh` | the same as `run_orchestrator.sh` | `./run_orchestrator_recovery.sh` |
| `test_tenancy.py` | running ws-server + Redis | see below |
| `test_replicas.py` | 2 ws-server replicas + Redis | see below |

The `run_*.sh` scripts build images, stand everything up, run the assertions, print the
resulting database rows where relevant, and **clean up after themselves on exit** —
including on failure.

---

## No stack required

Flight lifecycle event logic, with no Docker daemon and no media server:

```bash
docker run --rm -v "$PWD/orchestrator:/o:ro" -v "$PWD/tests/comms:/tests:ro" \
  -w /tmp -e ORCHESTRATOR_DIR=/o python:3.11-slim python /tests/test_orchestrator.py
```

The MediaMTX authorisation decision, with no database and no media server:

```bash
docker run --rm -v "$PWD/db_writer:/w" -v "$PWD/tests/comms:/tests:ro" \
  -w /tmp -e DB_WRITER_DIR=/w python:3.11-slim \
  sh -c "pip install -q pyjwt; python /tests/test_mediamtx_auth.py"
```

The Mosquitto authorisation decision, with no database and no broker:

```bash
docker run --rm -v "$PWD/db_writer:/w" -v "$PWD/tests/comms:/tests:ro" \
  -w /tmp -e DB_WRITER_DIR=/w python:3.11-slim \
  sh -c "pip install -q pyjwt; python /tests/test_mqtt_auth.py"
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
./run_orchestrator.sh        # 21 assertions, real containers spawned and torn down
./run_orchestrator_real_app.sh  # 7 assertions, the real GPU app, not the stub
./run_recording_upload.sh    # 8 assertions, real segment upload + DB traceability
./run_mqtt_auth.sh           # 9 assertions, real mosquitto-go-auth broker
./run_orchestrator_recovery.sh  # 13 assertions, real crash + restart
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

**`test_orchestrator.py`** — the event sequences MediaMTX actually produces, which is
where the bugs are: duplicate online and offline hooks, an offline immediately followed
by an online (a radio glitch, not a landing), a reconnect after the grace window, a key
revoked mid-flight, a container that fails to start, and shutdown while a teardown is
pending. Also pins the orchestrator→container env contract, including that **no**
`DB_USERNAME`/`DB_PASSWORD` reaches a flight container and that a stray base-env value
cannot redirect a tenant's annotated video. The same file also pins
`FlightOrchestrator.recover()` against a fake runtime: a still-running container is
reattached and behaves like any other live flight afterwards; one that already exited
while nothing was watching is closed out in db-writer and told to stop (removal); a
container carrying the `agrarian.flight_id` label but incomplete env is left alone
rather than guessed at; nothing is ever spawned twice.

**`run_orchestrator.sh`** — the same lifecycle with real MediaMTX hooks, a real Docker
daemon and real PostgreSQL. The "GPU app" is a stub image that sleeps: what is under
test is the orchestration, which is precisely what `FlightRuntime` makes testable
without a GPU.

One trap: the stub **traps SIGTERM**. A bare `sleep` as PID 1 ignores signals it has no
handler for, so `docker stop` blocks for the full 20 s stop timeout on every teardown —
which looks exactly like a hung orchestrator and cost a debugging round the first time.
The real app installs SIGTERM/SIGINT handlers, so the stub matches it.

**`run_orchestrator_recovery.sh`** — the part `test_orchestrator.py`'s fake runtime
cannot check: that a real orchestrator process really can be killed (not stopped —
`docker kill`, skipping the graceful-shutdown hook the same way an OOM kill or a host
reboot would) and still lose nothing. Two real flights are live beforehand; one
container is separately stopped while the orchestrator is down, simulating it exiting
on its own with nobody watching. A fresh orchestrator is then started and shown to:
track the still-running flight again with **no** duplicate container spawned, close
the exited one out in PostgreSQL and remove it, and — the recovered flight isn't just
present in a health-check, it still lands normally — tear it down cleanly once the
drone actually disconnects, same as any flight the orchestrator opened itself.

**`run_orchestrator_real_app.sh`** — the same lifecycle, but the container the
orchestrator spawns is the actual GPU app image (`APP_MODE=health_monitoring`,
`APP_GPUS=all`), with real ws-server, Redis, db-writer, MediaMTX and PostgreSQL behind
it. Proves what no other test here can: that the app, given only the orchestrator's
injected `FLIGHT_ID`/`PUBLISHER_TOKEN`/paths, actually reads `in/<key>`, runs its
pipeline on the GPU, and publishes to `out/<public_uuid>`. Needs
`nvidia-container-toolkit` and the `.pt` checkpoints already on disk (gitignored — see
`checkpoints/.gitkeep`). `danger_detection` mode is not covered: it requires a
TensorRT `.engine` built for the target GPU, which does not exist yet.

**`run_recording_upload.sh`** — the recording upload path, which the auth section above
explains was silently broken from the start (no `wget` on the default MediaMTX image).
Publishes straight to a real `out/<uuid>` rather than involving the GPU app — the
recording pipeline does not care what publishes, only that `record: yes` is set on that
path — then disconnects, which always flushes the current segment regardless of
`recordSegmentDuration`. Checks that MediaMTX's hook actually fires, the recorder
receives it and uploads (the `local` backend, since no cloud credentials are configured
here), and — the part that did not exist before this pass — that the segment is resolved
back to its `flight_id` and lands in the `recordings` table via the new `POST /recording`
endpoint, rather than only existing as a file under a UUID with no way to join it back to
a flight.

**`test_mqtt_auth.py`** — the Mosquitto analogue of `test_mediamtx_auth.py`, checked
against the same style of stub directory. The drone's stream key is the write
credential (mirrors publishing to `in/<stream_key>`: the topic's own key IS the
credential), and the app container's publisher token is the read/subscribe
credential — the same token already authorising the video ingest read, the
annotated-output publish, and alert writes. Includes the anchoring cases (topic
prefix/suffix smuggling, uppercase keys), scope confusion in both directions
(a viewer token cannot subscribe, a drone key cannot subscribe), and the case
that makes ACLs meaningful at all: a live, valid publisher token for flight 1
still cannot touch flight 2's topic, because the topic's stream-key segment,
not just the credential, is checked.

**`run_mqtt_auth.sh`** — the part the file above cannot check: that Mosquitto's
`mosquitto-go-auth` plugin is actually consulting db-writer and obeying it, over a
real broker with real `mosquitto_pub`/`mosquitto_sub`. MQTT does not give one
uniform "denied" signal, so three different ones are used: a refused CONNECT exits
5; a denied SUBSCRIBE returns exit 0 but prints "All subscription requests were
denied" (the exit code is as useless here as ffmpeg's against MediaMTX); a denied
PUBLISH at QoS 0 gives the client no signal at all, so the broker's own
`error code: 401` log line is counted before/after, the same technique
`run_mediamtx_auth.sh` uses for MediaMTX's log line. The strongest check doesn't
rely on any of those: a subscribed app container genuinely receives the exact
value its own drone published, on the exact topic scoped to their shared flight.

**`test_tenancy.py`** — the original leak. ws-server used to broadcast every alert,
including its JPEG and GPS position, to every connected client. Assertion 6 is the one
that matters: flight 2's viewer must receive **nothing**.

---

## Gaps

Mosquitto authorisation is covered (`test_mqtt_auth.py`, `run_mqtt_auth.sh`); MQTTS/TLS
is not — the listener block in `configs/mosquitto/mosquitto.conf` is commented out and
unexercised. See `CLOUD_ARCHITECTURE.md` §9 for current status.

`run_orchestrator_real_app.sh` drives the real GPU app tier through the orchestrator in
`health_monitoring` mode — ingest read, pipeline, and annotated-output publish are all
verified. `danger_detection` mode is not: it needs a TensorRT `.engine` built for the
target GPU, and none exists yet.

The recording upload path is verified for the `local` storage backend
(`run_recording_upload.sh`). The `azure` and `aws` backends in `recorder/main.py` are
still unverified — no test here has credentials to exercise them against a real Blob
container or S3 bucket. Per-tenant upload prefixes are also not implemented — every
segment lands at the storage root regardless of which tenant it belongs to.

The **recording upload path** has still never run — the hook that triggers it could not
execute until the `-ffmpeg` image tag landed, so nothing downstream of it is exercised.
