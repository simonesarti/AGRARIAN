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
| `test_session_tokens.py` | nothing | one-liner below |
| `test_mediamtx_auth.py` | nothing | one-liner below |
| `test_mqtt_auth.py` | nothing | one-liner below |
| `test_orchestrator.py` | nothing | one-liner below |
| `test_replication.py` | Postgres + 2 db-writer replicas | `./run_db_replication.sh` |
| `test_multiflight.py` | same | `./run_db_replication.sh` |
| `test_redis_reconnect.py` | Redis + 2 ws-server replicas | `./run_redis_failure.sh` |
| `test_redis_outage.py` | same | `./run_redis_failure.sh` |
| `run_mediamtx_auth.sh` | MediaMTX + db-writer + Postgres, host `ffmpeg` | `./run_mediamtx_auth.sh` |
| `run_orchestrator.sh` | the above + Docker socket, host `ffmpeg` | `./run_orchestrator.sh` |
| `run_orchestrator_real_app.sh` | the above + a GPU, `nvidia-container-toolkit`, `checkpoints/*`, Mosquitto | `./run_orchestrator_real_app.sh [mode]` |
| `run_recording_upload.sh` | MediaMTX + recorder + db-writer + Postgres, host `ffmpeg` | `./run_recording_upload.sh` |
| `run_mqtt_auth.sh` | mosquitto-go-auth + db-writer + Postgres | `./run_mqtt_auth.sh` |
| `run_portal_auth.sh` | Postgres + 2 db-writer replicas | `./run_portal_auth.sh` |
| `run_portal.sh` | Postgres + Redis + db-writer + 2 portal replicas | `./run_portal.sh` |
| `run_traefik_tls.sh` | the above + ws-server + MediaMTX behind a real Traefik | `./run_traefik_tls.sh` |
| `run_ingress_tls.sh` | MediaMTX + mosquitto-go-auth + db-writer + Postgres | `./run_ingress_tls.sh` |
| `run_orchestrator_recovery.sh` | the same as `run_orchestrator.sh` | `./run_orchestrator_recovery.sh` |
| `test_tenancy.py` | running ws-server + Redis | see below |
| `test_replicas.py` | 2 ws-server replicas + Redis | see below |
| `run_watch_live.sh` | **not a test** — the whole product plus a browser | `./run_watch_live.sh [host]` |

The `run_*.sh` scripts build images, stand everything up, run the assertions, print the
resulting database rows where relevant, and **clean up after themselves on exit** —
including on failure.

Every runner that mounts the real `mediamtx.yaml` or `mosquitto.conf` also issues a
throwaway certificate first, into a temporary directory rather than into
`certificates/` — whose CA may already be installed in a browser. That is not optional
politeness: both services now terminate their own TLS (§7), and MediaMTX **exits at
startup** if the certificate its config names is not on disk. Without it a runner fails
as a wall of connection errors that look like a networking problem.

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

Session tokens — the portal's credential, and its separation from the other two:

```bash
docker run --rm -v "$PWD/db_writer:/dbw" -v "$PWD/tests/comms:/tests:ro" \
  -w /tmp -e DB_WRITER_DIR=/dbw python:3.12-slim \
  sh -c "pip install -q pyjwt; python /tests/test_session_tokens.py"
```

## Orchestrated

```bash
./run_db_replication.sh      # 7 + 20×2 assertions, real PostgreSQL
./run_redis_failure.sh       # 3 + 7 assertions, Redis restarted mid-test
./run_portal_auth.sh         # 51 assertions, portal API across 2 replicas
./run_portal.sh              # 87 + 2 assertions, the portal driven as a browser
./run_mediamtx_auth.sh       # 27 assertions, real MediaMTX + ffmpeg publishes
./run_orchestrator.sh        # 21 assertions, real containers spawned and torn down
./run_orchestrator_real_app.sh                    # 15 assertions, the real GPU app in danger_detection
./run_orchestrator_real_app.sh health_monitoring  #  9 assertions, the same lifecycle in the other mode
./run_recording_upload.sh    # 8 assertions, real segment upload + DB traceability
./run_mqtt_auth.sh           # 9 assertions, real mosquitto-go-auth broker
./run_orchestrator_recovery.sh  # 13 assertions, real crash + restart
./run_traefik_tls.sh         # 22 + 9 assertions, the browser's half of the ingress tier
./run_ingress_tls.sh         # 42 assertions, the drone's half — RTMPS, RTSPS, MQTTS
```

`run_traefik_tls.sh` is the only runner that speaks HTTPS rather than working around
it. Every other one drives the portal over plain HTTP with `COOKIE_SECURE` left on,
which asserts the cookie's *attributes* but never that a browser would send it back —
a `Secure` cookie is not returned over `http://`. This one issues a certificate from
the local CA, puts the repo's own Traefik configuration in front of a real portal,
ws-server and MediaMTX, and drives the whole thing through the proxy.

`PORTAL_HOPS=0 ./run_traefik_tls.sh` fails exactly one assertion — the two-client
rate-limit bucket — and is how that assertion is kept honest.

`run_ingress_tls.sh` is its counterpart on the drone side: MediaMTX terminating
RTMPS and RTSPS and Mosquitto terminating MQTTS, with the same locally issued leaf.
It asserts two independent things, and the second is the one a transport change is
most likely to break quietly — that **authorisation is unchanged by the move**. A
revoked key must still be refused, another tenant's telemetry must still be
unreachable, and the plaintext fallback listeners must still work, because §7 keeps
them for drone firmware that cannot do TLS.

Its TLS-floor assertions use an OpenSSL old enough to still send a TLS 1.1
ClientHello, with a control that proves it can. Written with modern curl or OpenSSL
3 the same assertion passes against a server that happily *accepts* TLS 1.1, because
those clients refuse to offer one — it measures the client and not the server. That
was a live defect in `run_traefik_tls.sh`, found while writing this one and fixed
there too.

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
Also pins `active_flights()`, which resolves `/viewer/token`: only currently open
flights count (a landed one — `end_time` set — must not come back), and a lone
active flight resolves with nothing to disambiguate.

**`test_session_tokens.py`** — the portal credential's separation from the other two.
All three are signed with the same secret, so the `scope` claim is the only thing
keeping them apart, and it matters most here: a **viewer token already carries a `sub`
claim naming its user**, so without the scope check a token issued to watch one flight
would *be* a full account credential. Both directions are pinned, plus the property that
makes the reverse impossible by construction — a session token carries **no `flight_id`
claim at all**, so it cannot answer "which flight" even if a future caller forgets to
check scope. Also forged signatures, expiry, `alg:none`, a missing or non-numeric
subject, and five malformed `Authorization` headers.

**`run_portal_auth.sh` / `test_portal_auth.py`** — `/register`, `/login`, `/me` and the
stream CRUD routes over real HTTP against real PostgreSQL, with **two replicas**. The
second replica is the point rather than padding: a token minted on replica 1 must be
accepted by replica 2 with no shared session store, which is exactly what an in-memory
session would break — the defect ws-server had with its in-memory client set. Pins the
status codes the portal will branch on (409 duplicate or at-cap, 400 malformed, 401 bad
credentials, 404 not-yours) and that a failed login's body never says whether the
account exists.

For the stream routes it pins the tenancy rules the portal depends on: every route 401s
without a token and with a garbage one, another tenant gets the **same 404** for a
stream that is not theirs as for one that does not exist (`stream_id` is sequential, so
telling them apart would confirm a row exists), and their failed rotate leaves the
owner's key untouched.

The slot cap is checked three ways, the last being the one that matters: sequentially,
against the revoke → add → rotate revival bypass, and under **20 simultaneous adds
across both replicas**, which must create exactly 10. That last assertion was confirmed
non-vacuous by removing the row lock from `create_stream` and watching it create 11 —
there is no unique constraint to catch an overshoot here the way there is for a
duplicate email, so the lock is the only thing making the cap hold.

Registration (`create_user`) is pinned here too, since it is open to the internet and
every argument is untrusted: emails normalise on write *and* on read (PostgreSQL's
unique index is case-sensitive, so without both halves a user who registers as
`Alice@` and logs in as `alice@` is simply refused), duplicates are caught by the
constraint rather than a prior `SELECT` (check-then-insert is a race across replicas),
and passwords are bounded at bcrypt's 72-**byte** limit measured in bytes — one emoji
is four — because bcrypt 5.x raises past it and an unguarded long passphrase would be
a 500 rather than a clear rejection.

**`run_portal.sh` / `test_portal.py`** — the portal itself, driven the way a browser
drives it: form posts, a session cookie and an `Origin` header, against a real db-writer
and real PostgreSQL. **Two portal replicas**, for the reason above: a cookie issued by
replica 1 must be accepted by replica 2, and a server-side session would pass every
other assertion here and fail that one.

The replicas are started with **no `SESSION_JWT_SECRET` and no `DB_*` variables**, which
turns §7's claim into something the test can falsify: if the portal can still serve every
page, it validates nothing and reads no table itself. They also run with the production
cookie defaults (`Secure`, `SameSite=strict`, `HttpOnly`) even though the test speaks
plain HTTP — a browser would refuse to return a `Secure` cookie over `http://`, this
client does not, so the attributes can be asserted rather than quietly relaxed.

What it guards beyond the happy path:

- **The session token never reaches the page.** Asserted by searching the rendered HTML
  of the dashboard and the watch page for the cookie's value. The whole point of
  `httpOnly` is lost if the token is also printed into the document.
- **The viewer token is a downgrade.** What the watch page receives is checked to be a
  *different* token, and then spent against db-writer to prove it cannot act as a
  session or mint another viewer token — there is no path back up.
- **Cross-site request forgery**, four ways: a foreign `Origin` on add, sign-out and
  login are all refused, as is a state-changing POST carrying neither `Origin` nor
  `Referer`; a same-site `Referer` with no `Origin` is accepted. The refusals are then
  shown to have created nothing.
- **Label markup is escaped.** A slot labelled `<script>alert(1)</script>` must come
  back escaped: labels are tenant input rendered into HTML, and Jinja's autoescaping is
  the only thing between that and script execution in the owner's own tab.
- **The composed URLs**, since the portal is the only thing that knows how to build
  them: the WebRTC and HLS URLs point at the public media host and the flight's own
  `out/<public_uuid>` path, and the alert URL at ws-server. Whether video *plays* is not
  covered — that is WebRTC between the browser and MediaMTX, and the portal is not in
  the middle of it.

**Rate limiting** is the last section of the file, because exhausting a bucket cannot be
undone inside the window. The two replicas are started with **different proxy trust** —
`pt-1` trusts none, `pt-2` trusts one hop — which is the pair of configurations that
needs proving and also how each scenario claims a clean bucket: present an
`X-Forwarded-For` to `pt-2` and it is believed.

Five properties, each one a way the limit could be evaded if it were built the obvious
way:

- **Per account and per address are separate limits.** One account locked out does not
  lock out the next account from the same address (or a shared office NAT would take
  itself down), and twelve *different* accounts tried from one address are still refused
  (or spraying would walk straight past a per-account limit).
- **A success clears the account's counter, not the address's.** Four failures then a
  correct password then five more failures are all answered — otherwise a user who
  finally remembers their password stays locked out. The address counter deliberately
  survives, or an attacker holding one valid account could reset their own budget.
- **The counters are shared.** A bucket filled on replica 1 refuses on replica 2.
  Confirmed non-vacuous by pointing the replicas at different Redis databases and
  watching this assertion — and only this one — fail.
- **A forged `X-Forwarded-For` mints nothing** where no proxy is trusted, while the same
  header is believed by the replica configured for one hop. Confirmed non-vacuous by
  setting both replicas to trust one hop and watching the first half fail: the client
  then names its own bucket, which is not merely evasion — it is how one client locks
  another out.
- **Registration counts attempts, not accounts.** Eight *malformed* registrations still
  fill the bucket, because a 409 on a taken address is an existence oracle whether or not
  a row is created.

Two more assertions live in the runner rather than the Python file, because they need to
stop a container: with Redis stopped, a correct sign-in still returns 303 and a wrong one
still returns 401. **The limiter fails open on purpose** — one that turns a Redis outage
into "nobody can sign in" has become a worse outage than the attack it prevents.

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

It also exercises `/viewer/token` against a real db-writer and PostgreSQL, in two
parts. Disambiguation: alice starts a second concurrent flight, the plain request (no
`stream_id`) turns from 200 into 409 rather than silently picking one, `stream_id`
resolves each of her two flights correctly, and she cannot reach bob's stream_id.

And the credential it now requires — a **session token**, never a password. Alice and
bob log in first, exactly as the portal does. Email and password are refused where they
used to work, and so are a viewer token and a publisher token: the first would make a
viewer token self-renewing and therefore effectively permanent, and the second lives
inside a container that processes untrusted video. This is a deliberate credential
downgrade with no path back — a session token buys a flight-scoped one, and nothing
buys a session token except a password at `/login`.

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

**`run_orchestrator_real_app.sh [mode]`** — the same lifecycle, but the container the
orchestrator spawns is the actual GPU app image (`APP_GPUS=all`), with real ws-server,
Redis, db-writer, Mosquitto, MediaMTX and PostgreSQL behind it. Proves what no other
test here can: that the app, given only the orchestrator's injected
`FLIGHT_ID`/`PUBLISHER_TOKEN`/paths, actually reads `in/<key>`, runs its pipeline on the
GPU, and publishes to `out/<public_uuid>`. Needs `nvidia-container-toolkit` and the
checkpoints already on disk (gitignored — see `checkpoints/.gitkeep`).

**Both modes run.** The mode is the first argument and defaults to `danger_detection`.
This file used to hardcode `health_monitoring`, so the primary product mode had never
executed once; the note that used to sit here — that `danger_detection` needs a TensorRT
`.engine` built for the target GPU — was **wrong**. `danger_detection_stream.py` falls
back to the `.pt` detector and `.onnx` segmenter when no engine is present, and that is
the path this test takes. An engine is an accelerator, not a prerequisite.

`danger_detection` additionally starts **Mosquitto with the real `mosquitto.conf` and the
real db-writer ACL endpoint**, plus `telemetry_publisher.py` authenticating as the drone
with its stream key. That combination is what finally makes the telemetry plane carry a
message from the real app: `run_mqtt_auth.sh` proves who may publish where, but nothing
before this proved that a subscriber on the far side ever receives anything.

The load-bearing assertion is *"telemetry is reaching the combiner"*, and it is not
decoration. `FRAMETELCOMB_MAX_TIME_DIFF` is 150 ms, so a publisher can connect,
authenticate, be admitted by the ACL, and deliver every message on time — and still leave
every frame unmatched if it publishes below ~7 Hz. Verified by falsification: dropping the
publisher to 1 Hz leaves all fourteen other assertions green and fails only this one
(192 starved matches in the last 200 log lines). `health_monitoring` skips this whole
group because it never instantiates `FrameTelemetryCombiner`.

Assertions read `/app/logs/*.log` copied out of the flight container, not `docker logs` —
`app/main.py` configures no `StreamHandler`, so the container's stdout carries almost
nothing and the old `docker logs | grep CRITICAL` check could never fail. The runner sets
`APP_ENV_LOG_LEVEL=INFO`, without which the worker loggers sit at WARNING and every line
these assertions look for is dropped before reaching a handler.

`dem/dem.tif` is gitignored and usually absent; `open_dem_tifs()` returns `None` and the
GeoWorker skips slope and no-data analysis. Geofencing and the safety radius still run.
The script says which of the two it got, so a green run is not read as full geo coverage.

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
**both** modes — ingest read, pipeline, and annotated-output publish are verified for
each. `danger_detection` additionally verifies the telemetry plane end to end, against a
real broker enforcing the real ACLs.

Playback is verified too, though not by these scripts. HLS with `?jwt=` serves H.264
1920×1080 (`ffprobe`), and a real WHEP client (`aiortc`) gets a 201 and decodes 1920×1080
frames. What that leaves is the *page* rather than the protocol — autoplay policy, the
alert aside, small screens — which is what `run_watch_live.sh` is for.

**`run_watch_live.sh [host]`** — not a test. It stands the whole product up, puts a real
flight in the air, prints a URL and waits for you to look at it. Use it for anything that
needs a browser. Note that it runs the portal with `COOKIE_SECURE=false`, which is the
only way it works at all until the ingress tier exists.

The recording upload path is verified for the `local` storage backend
(`run_recording_upload.sh`). The `azure` and `aws` backends in `recorder/main.py` are
still unverified — no test here has credentials to exercise them against a real Blob
container or S3 bucket. Per-tenant upload prefixes are also not implemented — every
segment lands at the storage root regardless of which tenant it belongs to.

The **recording upload path** has still never run — the hook that triggers it could not
execute until the `-ffmpeg` image tag landed, so nothing downstream of it is exercised.
