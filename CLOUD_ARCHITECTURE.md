# Cloud Architecture — Drone Monitoring Service

**Status:** target architecture for the `secure-cloud` branch.
**Supersedes:** the previous per-session-isolated-stack document, which described a
different design (one MediaMTX, Mosquitto, ws-server and Traefik *per user session*)
and is no longer the direction. Nothing in that document should be treated as current.

Sections are marked with their implementation state:

- **[built]** — implemented and tested on this branch
- **[designed]** — decided, specified here, not yet written
- **[open]** — not yet decided

---

## 1. What the system does

A GPU-dependent processing application consumes a live video stream from a drone,
runs either **danger detection** or **herd monitoring** over it (selected by an
environment variable), and produces three outputs:

- an **annotated video stream**, republished for the user to watch live
- **alerts** — a message plus a JPEG crop and telemetry-derived position — pushed to
  the user's browser in real time and persisted to a database
- **recordings** of the annotated stream, archived to object storage

Around that application sits a set of shared services (the *communication hub*) that
move data in and out: a media server, an MQTT broker for telemetry, a WebSocket
server, a database writer, and a recording uploader.

---

## 2. Deployment model

Two tiers that scale on **different axes**. This is the central decision of the
architecture and the one most likely to be misread.

### The communication hub — scales on load

MediaMTX, Mosquitto, ws-server, db-writer, Redis and the recorder are **shared,
multi-tenant services**. One deployment serves every user. Replicas are added when
load demands it, not when a user signs up.

These are I/O-bound and cheap. Running a private copy per user would waste an order of
magnitude of resource and multiply the operational surface (certificates, DNS entries,
health checks, upgrades) by the user count.

### The application — scales by concurrent flight

The GPU application is **one container per active flight**. It is not multi-tenant and
should not become so: it holds model weights on a GPU, and its throughput is bounded by
that GPU. Two flights on one container means two streams contending for the same
device, with no isolation of failure and no way to schedule them independently.

A container exists only while a drone is actually streaming. It is created when the
stream goes live and destroyed when it stops.

### Why the two tiers are not the same number

The hub does not scale with user count because it does not need to. The app does,
because a GPU cannot be shared usefully. Collapsing them — spinning up a full private
stack per user — would mean paying for an idle MediaMTX, Mosquitto and ws-server per
user, and would still not solve anything the shared hub does not already solve. Isolation
between tenants is enforced by **authorization**, not by topology.

The consequence, and the reason the shared model requires more care than the old one:
in a per-session stack isolation is structural and free. Here it must be implemented
explicitly, and every shared component needs a tenancy story. Section 4 is that story.

```text
   drones                ONE SHARED HUB                    app tier
   ------         ----------------------------      --------------------

  drone A ─┐      ┌────────────────────────────┐    ┌──────────────────┐
  drone B ─┼─────▶│  MediaMTX   (1 deployment) │───▶│ app: flight A    │ GPU
  drone C ─┤      │  Mosquitto  (1 deployment) │───▶│ app: flight B    │ GPU
  drone D ─┘      │  ws-server  (N replicas)   │───▶│ app: flight C    │ GPU
                  │  db-writer  (N replicas)   │───▶│ app: flight D    │ GPU
  viewers ───────▶│  Redis, recorder           │    └──────────────────┘
                  └────────────────────────────┘      one per ACTIVE flight,
                    replicas follow LOAD,              created when the stream
                    never user count                   starts, destroyed when
                                                       it stops
```

One MediaMTX handles all four publishers. One Mosquitto handles all four telemetry feeds.
Only the GPU tier multiplies with flights.

### Platform: Kubernetes **[designed]**

The target is **managed Kubernetes** (AKS/EKS/GKE — not self-hosted; self-managing etcd is
not where a small team should spend attention). The current stack is docker-compose on a
single host, and there are no manifests yet.

**The deciding factor is GPU cost when nothing is flying.** One container per *active*
flight means the GPU tier is idle most of the day — drones fly in daylight, in workable
weather, seasonally. Plain Docker means renting GPU machines 24/7 and paying for all of
it. A Kubernetes GPU node pool scaled to **min = 0** creates machines when a flight starts
and destroys them when it ends. At cloud GPU prices that difference dominates every other
consideration in this decision.

Two supporting reasons:

- **Docker alone cannot schedule across hosts.** One host means one GPU, so concurrency is
  capped at whatever fits on a single card. Swarm is not the answer — weak GPU support and
  effectively in maintenance.
- **A flight is a finite workload**, so it maps to a Kubernetes **Job**, not a Deployment.
  Retry semantics and cleanup come with it.

#### Why not serverless containers

Cloud Run, Azure Container Apps and ECS Fargate all offer scale-to-zero with GPUs and would
avoid the Kubernetes learning curve. For the GPU tier in isolation they would work.

**MediaMTX rules them out.** It needs UDP/8189 for WebRTC media and raw TCP for RTMP/RTSP
ingest; those platforms route HTTP only. MediaMTX would have to live on a VM anyway, leaving
two deployment models to operate at once — worse than either alone. This constraint is
non-obvious and eliminates the option that otherwise looks best for a small team, so it is
recorded here rather than rediscovered later.

#### Migration is mechanical, with one real change

Every hub service already has a Dockerfile and takes configuration from environment
variables, so compose services convert to Deployments directly. The one genuine difference
is that **MediaMTX and Mosquitto need `LoadBalancer` services carrying TCP and UDP**, not an
HTTP Ingress — which is the same split already chosen for TLS termination in §7, so the
topology and the security model agree.

Managed Kubernetes also brings cert-manager, which closes the TLS item still open in §9.

#### Build the orchestrator against an interface, not a cluster **[built]**

The orchestrator targets a two-method abstraction:

```python
class FlightRuntime(Protocol):
    def start(self, flight_id: int, env: dict) -> str: ...   # returns handle
    def stop(self, handle: str) -> None: ...
```

The **Docker backend** is built and tested — `DockerFlightRuntime`, runnable on a laptop
against `/var/run/docker.sock`. It unblocked the whole flight lifecycle (stream live →
hook → container → stream stops → container gone) with no cloud account involved. The
Kubernetes backend (create Job, delete Job) is a comparable amount of code and lands at
deployment time.

This was not indecision, and the split held up: the orchestrator's hard part turned out
to be exactly the lifecycle logic — reconnects, duplicate hooks, failed starts — none of
which is platform-specific, and all of which is tested against a fake runtime with no
container daemon in sight.

---

## 3. Identity and credentials

Three credential types, one per class of client. They differ because the constraints on
the party presenting them differ — this is deliberate, not inconsistency.

| Channel | Credential | Lifetime | Why this form |
| --- | --- | --- | --- |
| Publisher → MediaMTX | **Stream key** | Until revoked | Typed by hand into a controller before each flight; must be short |
| Browser → WebRTC / HLS / WebSocket | **JWT** | Hours | Carried by software; length is free, expiry is free |
| App container → hub | **Injected token** | Container lifetime | Never touched by a human |

### Stream keys **[built]**

The operator types the ingest URL into the drone controller before every flight. That
single constraint determines the design: the credential must be short enough to type
without error. A JWT is 200+ characters and expires, so it is unusable here.

A stream key is therefore ~16 characters of unambiguous base32 (~80 bits), **persistent
until revoked or rotated**, and scoped to **one stream** rather than one user. It is not
weaker than a session token — it trades automatic expiry for instant revocation, which
is the more useful property for a credential that is configured once and left in place.

Per-stream scoping is required by **concurrency**, not by hardware tracking: the key
doubles as the ingest path, so one key means one path, and a user running two feeds
simultaneously would have them collide. A `streams` row identifies no airframe. A drone
that changes hands is not transferred — its new owner simply adds a stream of their own,
with an unrelated key, and the previous owner retires theirs.

The key doubles as the ingest path:

```text
rtmps://ingest.<host>:1936/in/k7m2q9xr4td8vnc3
```

This is only safe because **the ingest path is never a path a viewer touches**. The app
republishes to a separate output path derived from the flight, and viewers authenticate
separately with a JWT. If those two paths were ever unified, the stream key would leak to
every viewer.

The residual cost is that the key appears in MediaMTX access logs. Revocability is what
covers that.

### Viewer tokens **[built]**

A short-lived JWT (HS256), minted by db-writer, naming exactly one `flight_id`. ws-server
validates it offline — signature and expiry only, no database or network call — so any
replica can authorise any viewer. The token travels in the WebSocket query string, because
browsers cannot set headers on a handshake; that is why it is short-lived.

The same token now gates the **MediaMTX read** as well, through the auth hook in §4, so
the annotated video and the alerts describing it are protected by one credential.
Verified end-to-end: an authorised viewer receives the HLS manifest of a live stream
while a second tenant's valid viewer token is refused on the same path.

The browser presents it as `Authorization: Bearer` to WebRTC/HLS and in the query string
to the WebSocket — the same token, two carriers, because a WebSocket handshake cannot
carry headers.

`flight_id` is an autoincrement primary key and therefore guessable. **The signature, not
the identifier, carries authority.** No identifier in this system is ever a credential.

### Publisher tokens **[built]**

An app container presents a **per-flight JWT**, minted by db-writer when the flight opens
and returned once from `/flight/open`, the endpoint the orchestrator calls with the
stream key MediaMTX gave it. The same token is accepted by db-writer and ws-server, so
there is one credential and one mechanism rather than two.

Every write endpoint compares the `flight_id` in the URL against the claim, so a token
issued for flight 7 cannot be replayed against flight 8. This replaced a single
`WS_PUBLISHER_TOKEN` shared by every container, which authorised writing to *any* flight
and was therefore a network-boundary check rather than tenant isolation. It also closed
db-writer's alert endpoint, which previously had no credential at all.

Viewer and publisher tokens are signed with the same secret, so each carries a **`scope`
claim** (`view` / `publish`) that is checked on every path. That check is load-bearing:
without it a viewer token would be a valid publisher token for the flight being watched,
letting anyone with read access inject alerts into it.

---

## 4. Component tenancy

> **Read the "Instances" column first.** Everything below except the GPU app is a
> **single shared deployment** serving all users at once. Phrases like "per-flight JWT"
> or "channel per flight" describe how one shared service *separates tenants internally* —
> they do **not** mean a copy of that service exists per flight. Exactly one row in this
> table is instanced per flight.

| Component | Instances | Tenancy mechanism | State |
| --- | --- | --- | --- |
| **GPU app** | **One per active flight** | Sole occupant — no internal tenancy needed | container **[built]**, lifecycle **[built]**, paths **[verified]** for `health_monitoring`; `danger_detection` needs a TensorRT engine |
| MediaMTX | Shared, replicated on load | Regex paths + HTTP auth hook | **[built]** |
| Mosquitto | Shared, replicated on load | Per-stream credentials + topic ACLs | **[built]** |
| ws-server | Shared, replicated on load | Per-flight JWT (view + publish scopes); Redis pub/sub fan-out | **[built]** |
| db-writer | Shared, replicated on load | Stateless per request; bcrypt user auth | **[built]** |
| Redis | Shared | Channel per flight (`flight:{id}`) | **[built]** |
| Recorder | Shared | Segment → flight_id resolved via `recordings` table | upload **[built]**, per-tenant prefix **[open]** |
| Orchestrator | Shared | Spawns/stops app containers | **[built]** |
| Portal | Shared | Session cookie → user | **[open]** |

A single MediaMTX serves every drone publishing and every viewer watching; a single
Mosquitto carries every publisher's telemetry. They are separated by path regex, credentials
and ACLs — not by having one broker each.

> **db-writer holds no per-flight state.** Every endpoint works from the `flight_id` in
> the URL plus the database, so any replica serves any flight regardless of which one
> opened it. The only process-local object is `AlertWriter` — a queue and a thread that
> keep database latency off the caller's hot path — and it is flight-agnostic, so a
> replica accepts alerts for flights it has never seen.

### ws-server **[built]**

Previously broadcast every alert — including the JPEG and position — to every connected
client. Now maintains a per-flight session map, and because horizontal replicas cannot
share an in-memory client set, fan-out goes through Redis pub/sub.

Replicas **subscribe selectively** to the flights they actually have viewers for, rather
than pattern-subscribing to everything. With base64 JPEGs in the payload, pattern
subscription would ship every tenant's imagery to every replica.

#### Redis failure behaviour **[verified]**

Tested against two live replicas with a viewer connected throughout — first a fast restart,
then a sustained full outage (Redis stopped, ~15 s down, restarted).

| Behaviour | Result |
| --- | --- |
| Reader survives the broker vanishing | Raises `ConnectionError`, caught, retried on `REDIS_RETRY_DELAY` |
| Resubscribes on reconnect | **Yes** — redis-py re-issues SUBSCRIBE for its channels |
| Viewer must reconnect | **No** — the same WebSocket keeps receiving afterwards |
| Publish while Redis is down | Fails loudly with HTTP 500, never a silent success |
| Alerts published during the outage | Lost, not replayed — best-effort by design |
| Viewer socket during the outage | Stays open; no spurious disconnect |

This was previously an untested assumption and is the reason a single Redis instance is
tolerable: the failure mode is a bounded gap in live delivery, not a stuck or silently dead
subscriber, and db-writer persists every alert independently regardless.

**Nothing is cached and nothing is replayed.** A viewer receives only alerts raised while
it is connected, and starts on a blank screen. An alert asserts something about the field
*now*; replaying the last one to a fresh connection would state something that may no
longer be true, with no cue that it is old. History belongs in the database, where every
alert carries its timestamp.

Two ports, and the separation is a security boundary: the WebSocket port is proxied
externally; the alert-write API port must never be routed from outside the cluster.

### MediaMTX **[built]**

MediaMTX's built-in `authInternalUsers` is a **static list in the config file**. That does
not survive user 101 arriving while 100 people are streaming. The fix is to give MediaMTX
a question to ask rather than a roster to hold:

```yaml
authMethod: http
authHTTPAddress: http://db-writer:8000/auth/mediamtx

paths:
  # ingest — $G1 is the stream key. Crockford base32: no i, l, o or u.
  "~^in/([0-9abcdefghjkmnpqrstvwxyz]{16})$":
    source: publisher
    # runOnAvailable / runOnUnavailable land with the orchestrator — see below.

  # annotated output — viewers read here. $G1 is the flight's public_uuid.
  "~^out/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$":
    source: publisher
    record: yes
```

The ingest regex is generated from `STREAM_KEY_ALPHABET` and `STREAM_KEY_LENGTH` in
`db_writer/constants.py` on the Python side, so a key MediaMTX would reject is a key
that could never have been minted. The two must be edited together.

There is **no catch-all path**: a path matching neither pattern is rejected by MediaMTX
before authentication is consulted at all. The old fixed `drone` and `annot` paths are
gone — they were shared by every tenant and readable by anyone who knew the name.

`runOnAvailable`/`runOnUnavailable` are live and point at the orchestrator, which is
built (§6). They were commented out for as long as the orchestrator did not exist,
since a hook aimed at an absent service fires a failing command on every flight.

Every protocol MediaMTX can listen on is now set explicitly. v1.19 enables **SRT (8890)
and MoQ (8892)** by default; neither was published in compose, so nothing was reachable
from outside, but both were running inside the container on every start — MoQ generating
a self-signed certificate each time — and a later compose change publishing a port range
would have exposed them with nobody having decided to. `srt: no` and `moq: no` close that.

MediaMTX POSTs `{user, password, token, ip, action, path, protocol, id, query, userAgent}`
on every connection attempt; any 2xx allows, anything else denies. New users work the
instant their row exists — no restart, no config reload, no roster.

Choosing HTTP auth over MediaMTX's JWT method is what keeps the credential form open: the
endpoint decides what a valid credential looks like, so a future ground-station app that
*can* fetch a short-lived token is a change to one Python function, not to media server
configuration.

#### The four legitimate combinations

There are exactly four, each with a different credential. Everything else is denied,
including any action MediaMTX may add in future — unrecognised actions arrive closed.

| Action | Path | Who | Credential |
| --- | --- | --- | --- |
| publish | `in/<stream_key>` | the drone | the path **is** the credential |
| read | `in/<stream_key>` | the app container | publisher token for a flight opened on **that stream** |
| publish | `out/<public_uuid>` | the app container | publisher token for **that flight** |
| read | `out/<public_uuid>` | the viewer | viewer token for **that flight** |

The two `out/` rows differ only by scope claim, which is what stops a viewer token from
being a publisher token for the flight it is watching. The `read in/` row compares the
token's flight against the *stream* the key names, so a live publisher token cannot open
somebody else's raw drone feed.

The denial reason is logged and never returned: a caller learning whether a stream key
exists, or that a token was merely for the wrong flight, learns something about another
tenant.

**Consequence:** this endpoint is on the critical path for every publish and every read.
It needs more than one db-writer replica. Caching is deliberately **absent** — a cache on
an authorisation decision delays revocation, and revocability is the property stream keys
are built on, since they never expire. That trade is not yet decided; see §9.

**MediaMTX's HLS server redirects before it authenticates.** The first request answers 302
to `?cookieCheck=1` and only the followed request reaches the auth hook. Any client — or
test — that does not follow redirects and keep cookies sees 302 for everything and never
learns whether it was authorised.

**Auth and spawn are separate events.** The auth hook fires on every connection attempt,
including aborted and retried ones. Spawning GPU containers from it would spawn them for
drones that never stream. The spawn belongs on `runOnAvailable`.

#### The image tag is load-bearing **[built]**

The stack must run **`bluenviron/mediamtx:latest-ffmpeg`**, and not for ffmpeg.

The default image contains three files — the binary, a config and a licence. No shell,
no `wget`. MediaMTX execs `runOn*` commands directly rather than through a shell, so on
that image every hook fails with:

```text
runOnAvailable command exited: exec: "wget": executable file not found in $PATH
```

MediaMTX logs that at INF and carries on. **This is why recordings were never uploaded:**
`runOnRecordSegmentComplete` pointed at the recorder sidecar from the start but never
once fired until this was found. The `-ffmpeg` tag is Alpine based and supplies busybox
`wget`, which posts `application/x-www-form-urlencoded` — the encoding the orchestrator's
`Form(...)` endpoints expect. Fixed and verified end to end — see §9.

The same constraint rules out shell syntax in any hook: no pipes, no `&&`, no redirects.
`-O /dev/null` is an argument, which is why it works.

### Mosquitto **[built]**

Was `allow_anonymous true` with no ACLs, and every telemetry topic flat
(`telemetry/latitude`) with no per-flight scoping at all — so two concurrently active
flights would each receive the other's telemetry on the shared broker, independent of
the missing authentication.

Fixed with `mosquitto-go-auth`'s HTTP backend rather than the dynamic-security plugin:
the latter's credential/ACL store is a JSON file the broker owns, a second store that
would need pushing and keeping in sync with the `streams` table on every add/rotate/
revoke — the same static-list defect `authInternalUsers` had. The HTTP backend instead
mirrors the MediaMTX design exactly: db-writer is asked live on every CONNECT and every
PUBLISH/SUBSCRIBE (`db_writer/mqtt_auth.py`, `/auth/mqtt/user` + `/auth/mqtt/acl`), so
the streams table stays the single source of truth and revocation needs no reload.

Topics are namespaced `telemetry/<stream_key>/<field>` so the ACL check can actually
separate tenants — the drone's stream key is the publish credential (the topic's own key
IS the credential, like `in/<stream_key>` on the video plane), and the app container
reuses its existing publisher token to subscribe, the same token already authorising the
video ingest read, the annotated-output publish, and writing alerts. See §9 for what was
verified and the caveat that the upstream plugin project is now archived.

---

## 5. Data model

```text
User 1 ──<N Stream 1 ──<N Flight 1 ──<N Alert
```

A **stream is a concurrency slot, not an aircraft** — one ingest credential the user
can publish on. Users add slots as the number of simultaneous feeds they need grows,
and retire them when it shrinks. Nothing in the schema models a physical drone.

Strictly linear. A flight carries **no `user_id`** — the owner is reached through
`streams.user_id`. A redundant column could contradict the stream's, and nothing in
the schema would say which one was authoritative.

| Table | Key columns | Notes |
| --- | --- | --- |
| `users` | `user_id` PK, `email`, `password` (bcrypt) | **[built]** |
| `streams` | `stream_id` PK, `user_id` FK, `stream_key` unique, `label`, `revoked_at` | **[built]** |
| `flights` | `flight_id` PK, `stream_id` FK (NOT NULL), `public_uuid` unique, `output_path`, `end_time` | **[built]** |
| `alerts` | `alert_id` PK, `flight_id` FK, JPEG, dimensions, timestamps | **[built]** |

`flight` is the tenancy unit throughout — it scopes alert rows, WebSocket delivery, Redis
channels and the output path. One user running two feeds at the same time holds two
streams and therefore produces two independent flights.

`revoked_at` retires a slot without deleting anything. "Remove" in the portal means
revoke plus hide — the row, its flights and their alerts all survive, and the key stops
resolving immediately. Rows are never hard-deleted; the `streams → flights` cascade
exists only for full account erasure.

`output_path` records the media-server **path** of the annotated output (`out/<public_uuid>`),
set the moment the flight opens. A path rather than a full URL, and never the ingest one:
a URL would carry the app's own publisher token in its query string, writing a live
credential into a row the portal reads, and the ingest path embeds the stream key, which
would scatter live credentials through flight history and leave dead ones behind after
every rotation. The host a viewer dials is not the host the app publishes to, so the
portal composes the viewer-facing URL from this path and the public media hostname it
knows.

`public_uuid` backs the `out/<uuid>` output path in §4. It must be random rather than
derived from `flight_id`: the sequential PK would make every tenant's output path
enumerable, and read authorization is the only thing standing in front of it.

`end_time` is stamped when the orchestrator tears the flight down, and never overwritten
once set — a stream that drops and reconnects can deliver a late teardown for a flight
that already closed, and the first timestamp is the true one. NULL means "still in the
air", which is also what it means for a flight whose orchestrator died before it could
close, so it is **not** a liveness signal.

**Rebuilding:** `db_writer/rebuild_schema.py --drop` drops and recreates everything,
optionally seeding a user and one stream slot. It is destructive by design and was
written while the database held only test data; once there is real data in it, changes
need a migration instead, because SQLAlchemy's `create_all` adds tables but never alters
existing ones.

---

## 6. Flight lifecycle

Steps 3–6 and 8 are **[built]**; 1–2 wait on the portal. Step 6 is verified for
`health_monitoring` mode; `danger_detection` mode still needs a TensorRT engine for the
target GPU — see §9.

1. User registers on the portal → row in `users`. **[open]**
2. User adds a stream → row in `streams` with a generated `stream_key`, shown once. The
   portal offers rotate and retire. **[open]**
3. Operator types `rtmps://ingest.<host>:1936/in/<key>` into the controller.
4. Publisher connects. MediaMTX POSTs `{action: "publish", path: "in/<key>"}` to db-writer,
   which resolves the key and checks `revoked_at IS NULL` → 200.
5. Stream goes live. `runOnAvailable` fires with `$G1` = the key. The orchestrator calls
   db-writer's `/flight/open`, which **creates the flight row** and mints a publisher
   token, then spawns the container with `flight_id`, ingest path, output path and token
   injected as environment.
6. App reads `in/<key>`, publishes annotated video to `out/<public_uuid>`, POSTs alerts to
   ws-server and db-writer. **[verified for `health_monitoring` mode against a real GPU
   container — see §9; `danger_detection` mode still needs a TensorRT engine]**
7. Viewer opens the portal, receives a JWT scoped to that flight, and presents it for the
   WebRTC/HLS read and the WebSocket connection alike. MediaMTX validates it through the
   same auth endpoint with `action: "read"`.
8. Publisher disconnects. `runOnUnavailable` → container stopped, `end_time` stamped.

**The app container never sees end-user credentials.** It used to call `/session/start`
with the user's email and password, putting a reusable account credential inside a
container that processes untrusted video. The orchestrator now opens the flight and
injects only the result — which also removes the "DB session start failed → abort the
run" coupling, since the app no longer authenticates anyone.

### A dropped stream is not usually a finished flight **[built]**

MediaMTX fires `runOnUnavailable` the instant a publisher disconnects, and it reports a
momentary radio glitch exactly the way it reports a landing. Tearing down immediately
would mean a cold GPU start — model weights reloaded from disk — for a blip.

Teardown is therefore deferred by `RECONNECT_GRACE_S` (default 30 s) and cancelled if the
same key comes back. Reconnecting inside the window keeps the same flight, the same
container and the same `flight_id`; reconnecting after it is a new flight, which is the
honest description of what happened.

Both hooks can fire more than once and can interleave, so every operation on one stream
key serialises behind its own lock — per key, not global, so two tenants taking off at
the same moment do not queue behind each other's container start. Both entry points are
idempotent.

If the container fails to start, the flight row is closed immediately rather than left
looking live forever.

---

## 7. Security model

### Trust boundaries

| Boundary | Exposure | Protection |
| --- | --- | --- |
| Publisher → MediaMTX | Public internet | RTMPS/RTSPS; stream key; **plain RTMP retained only for drones without TLS support** |
| Telemetry → Mosquitto | Public internet | MQTTS; per-stream credentials + topic ACLs; plain MQTT as the same narrow fallback |
| Browser → hub | Public internet | HTTPS/WSS at the reverse proxy; per-flight JWT |
| App ↔ hub | Cloud virtual network | Not separately encrypted — see below |
| Hub → database | Private | Worker credentials, least privilege |

### TLS termination

Traefik terminates HTTPS and WSS for the HTTP-family services. **MediaMTX and Mosquitto
terminate their own TLS**, for two reasons: it preserves the client identity that ACLs and
the auth hook depend on, and WebRTC media is DTLS-SRTP over UDP end-to-end, so it bypasses
an L7 proxy entirely. Only WHEP signalling is HTTP.

(Traefik *does* support TCP routers with SNI, so proxying RTMPS is possible — self-termination
is chosen for the identity-preservation reason, not because of a Traefik limitation.)

### In-cloud traffic

App↔hub traffic crosses the cloud provider's virtual network and is accepted unencrypted.
This is a deliberate scoping decision, not an oversight. It holds only while both tiers are
in the same trust domain; it does **not** hold for the current interim deployment, where the
app runs on a laptop and reaches the hub over a VPN.

### Secrets

`SESSION_JWT_SECRET` is the only shared secret, carried by db-writer (which mints) and
ws-server (which validates). It is required at startup via a `${VAR:?}` guard — the stack
refuses to start rather than defaulting to something permissive. Generated with
`openssl rand -hex 32`. `.env` is gitignored; `.env.example` documents every variable
without values.

There is **no pre-shared publisher secret**. App containers receive a token scoped to
their own flight when the flight opens, so no long-lived credential is distributed to the
GPU tier at all.

---

## 8. Network topology

Externally reachable:

| Port | Protocol | Terminated by |
| --- | --- | --- |
| 1935 | RTMP (fallback only) | MediaMTX |
| 1936 | RTMPS | MediaMTX |
| 8554 / 8322 | RTSP / RTSPS | MediaMTX |
| 8888 | HLS | Traefik |
| 8889 | WebRTC / WHEP signalling | Traefik |
| 8189/udp | WebRTC media | End-to-end DTLS-SRTP — **must not be proxied** |
| 1883 / 8883 | MQTT (fallback) / MQTTS | Mosquitto |
| 443 | HTTPS + WSS | Traefik |

Internal only — **must never be routed from outside**: ws-server's alert-write API port,
db-writer, Redis, the recorder, and the orchestrator.

> - **[built]** The port constants in `app/shared/processes/constants.py` are corrected.
>   `RTMPS_PORT` and `RTSPS_PORT` now carry MediaMTX's actual defaults (1936, 8322)
>   rather than 8443 and 441, and `HTTPS_PORT`/`WSS_PORT` are 443 rather than 8443,
>   which is what this table says Traefik terminates and what removes the collision
>   between them. `WEBSOCKET_PORT` no longer derives from `HTTPS_PORT` — it is
>   ws-server's WebSocket listener (8765) and had nothing to do with HTTPS. None of
>   these names is read by any code path today (the app reaches its services through
>   `app_settings.py`), so this was latent throughout and is now simply correct.
> - **[built]** SRT and MoQ are disabled explicitly in `mediamtx.yaml` — see §4.
> - **[note]** compose publishes ws-server's alert-write API on host `8001` and db-writer
>   on `8002`, which this section says must never be routed from outside. That is the
>   interim laptop-app deployment described in §7, not the target topology.

---

## 9. Outstanding work

> Tests backing the claims below live in **`tests/comms/`**, with a README covering what
> each one guards and how to run it. Three shell runners stand up the required containers
> and clean up after themselves. The host interpreter has none of the dependencies, so
> everything runs in throwaway containers — except `run_mediamtx_auth.sh`, which needs
> `ffmpeg` and `curl` on the host to drive real publishes and reads.

### Built and tested

- ws-server per-flight isolation, Redis fan-out across replicas, and viewer JWT
  validation. Verified by 11 tenancy tests and 2 cross-replica tests, including
  confirmation that a second tenant's viewer receives nothing.
- **MediaMTX authorization on both publish and read.** db-writer's `/auth/mediamtx`
  endpoint, the regex paths, and the removal of the shared `drone`/`annot` paths.
  Verified by 40 assertions on the decision itself and 16 end-to-end against a real
  MediaMTX with real RTMP publishes and HLS reads, two tenants throughout: a live key
  publishes, an unknown or revoked one does not, an authorised viewer receives the HLS
  manifest of a live stream, and a second tenant's *valid* viewer token is refused on
  the same path.
- **Per-flight publisher tokens on both write paths.** db-writer's alert and
  flight-close endpoints now require one, as does ws-server's alert endpoint. Verified
  by 17 tests across both implementations: scope separation both ways, cross-flight
  replay, forged signatures, expiry, malformed and scopeless tokens.
- **Flight lifecycle end to end** (§6). The orchestrator, its Docker backend, db-writer's
  `/flight/open` and `/flight/{id}/close`, and MediaMTX's availability hooks. Verified by
  39 assertions on the event logic against fakes — duplicate hooks, reconnect inside and
  outside the grace window, revoked keys, a container that fails to start, shutdown with
  a teardown pending — and 21 end-to-end with real MediaMTX hooks, a real Docker daemon
  and real PostgreSQL: ffmpeg starts publishing and a container appears carrying the
  flight's paths and token and **no** end-user credentials; the publisher drops and
  returns and the same container survives; it lands and the container is gone with
  `end_time` stamped; a revoked key spawns nothing.
- **Redis reconnect and outage recovery** (§4). 3/3 on fast restart, 7/7 on a sustained
  outage: automatic resubscribe, viewer never reconnects, publishes fail loudly while down,
  no replay afterwards.
- **db-writer replica safety.** The per-flight `DatabaseManager` dict was replaced by a
  process-wide `AlertWriter` and stateless endpoints. Verified against two live replicas
  and a real PostgreSQL instance: a flight opened on replica 1 (via `/flight/open`, the
  same call the orchestrator makes) accepted an alert and a close on replica 2, all rows
  reached the database, and 40 interleaved alerts across two concurrent flights and both
  replicas persisted with no loss and no cross-contamination. Auth held across replicas
  throughout.
- **The real app tier, driven by the orchestrator, in `health_monitoring` mode.**
  `run_orchestrator_real_app.sh` builds the actual GPU app image (not the sleeping stub)
  and runs it with `--gpus all` behind the orchestrator: a live publish spawns it with
  the injected `FLIGHT_ID`/`PUBLISHER_TOKEN`/paths, it reads `in/<key>`, runs the full
  tracking → anomaly → interpolation → annotation pipeline on the GPU with zero
  `CRITICAL` log lines, publishes annotated video to its own `out/<uuid>` (confirmed by
  MediaMTX's own "is publishing to path" log line), and is torn down cleanly on landing
  with the flight row closed. 7/7 assertions, real MediaMTX/db-writer/ws-server/Redis/
  Postgres throughout. No alert was expected or produced: the input is an ffmpeg
  `testsrc` pattern with no trajectories to flag. `danger_detection` mode remains
  unverified — see the gap above.
- **The recording upload path, end to end, with database traceability.**
  `runOnRecordSegmentComplete` pointed at the recorder sidecar from the start but never
  fired until the `-ffmpeg` tag fix (see the auth section above); until now nothing had
  driven it. `run_recording_upload.sh` publishes to a real `out/<uuid>`, disconnects
  (which always flushes the current segment regardless of `recordSegmentDuration`), and
  confirms MediaMTX's hook fires, the recorder receives it and uploads to the `local`
  backend, and — new this pass — the segment is resolved back to its `flight_id` and
  logged in a new `recordings` table via a new `POST /recording` endpoint, rather than
  only existing as an anonymous file under a UUID nobody can join to a flight. 8/8
  assertions, including the file actually present on the shared volume. Azure/AWS
  backends remain unverified — no credentials are configured to test against them.
- **Mosquitto authorisation and per-flight telemetry isolation.** Previously
  `allow_anonymous true` with no ACLs, and every telemetry topic flat
  (`telemetry/latitude`) regardless of which drone or flight it belonged to — so two
  concurrently active flights would each receive the other's GPS and gimbal data on
  the shared broker. Topics are now namespaced per stream
  (`telemetry/<stream_key>/<field>`), and a new `mosquitto-go-auth` HTTP backend
  (`db_writer/mqtt_auth.py`, `/auth/mqtt/user` + `/auth/mqtt/acl`) mirrors
  `/auth/mediamtx` exactly: the drone's stream key is the publish credential, and the
  app container reuses its existing publisher token to subscribe — a fourth thing
  that one token now authorises, not a new credential. Requires the
  `iegomez/mosquitto-go-auth` image in place of stock `eclipse-mosquitto`, since the
  plugin does not ship in the stock image. Verified by 27 assertions on the decision
  itself (`test_mqtt_auth.py`) and 9 end-to-end against a real broker
  (`run_mqtt_auth.sh`): CONNECT refused for an unknown/revoked key or garbage
  credential, SUBSCRIBE denied for another tenant's topic, PUBLISH denied for another
  tenant's key (checked via the broker's own log line — MQTT gives no client-side
  signal for a denied QoS-0 publish, the same trap as ffmpeg's exit code against
  MediaMTX), and a subscribed app genuinely receiving the exact value its own drone
  published. The upstream plugin project is archived (no longer maintained) as of
  mid-2025; it still works and is pinned to a specific image tag, but has no
  security-patch path if a CVE surfaces.
- **Orchestrator restart recovery.** Flight state lived only in orchestrator memory;
  a crash or a forced kill — anything that skips the graceful-shutdown hook, which is
  what an OOM kill, `docker kill`, or the host rebooting all do — left running flight
  containers orphaned (never torn down, using GPU forever) and their flight rows open
  forever (no teardown was ever coming to close them). Recovery needs no database:
  `PUBLISHER_TOKEN` and the stream paths the orchestrator itself injected at
  container-start time are still sitting in the container's own environment, the one
  piece of state a restart cannot lose, so `FlightOrchestrator.recover()` reads them
  back via the `agrarian.flight_id` Docker label instead. It runs before the HTTP
  server accepts its first request, so no online/offline hook can race it. A container
  still running is reattached and behaves exactly like any other live flight from then
  on; one that already exited while nothing was watching is closed out and removed by
  recovery itself, since no offline hook will ever arrive for it. Verified by 9 new
  assertions on `FlightOrchestrator.recover()` against a fake runtime (reattachment,
  closing an already-exited container, skipping a container with incomplete env rather
  than guessing, no duplicate spawn) and 13 end-to-end (`run_orchestrator_recovery.sh`):
  two real flights running, the real orchestrator container `docker kill`ed (not
  stopped — no graceful shutdown runs), one flight's container separately stopped
  while the orchestrator is down, a fresh orchestrator started, and confirmation that
  the still-running flight is tracked again with no duplicate container while the
  exited one is closed in PostgreSQL and removed — then the recovered flight lands
  normally afterwards, same as any flight the orchestrator opened itself.
- **`/viewer/token` no longer guesses which flight.** It used to return whichever
  flight a user started most recently — wrong two different ways at once: it could
  hand out a token for a flight that had already landed (start time is not a liveness
  signal), and it silently picked one out of several once a second stream could be
  active at the same time, rather than asking which, now that concurrent flights are
  a real supported case. Both were the same underlying bug (`latest_flight_id`
  conflated "most recent" with "active"), fixed by one query:
  `UserDirectory.active_flights()` returns only currently open flights
  (`end_time IS NULL`). With zero there is nothing to hand out (404); with exactly
  one — still the common case — nothing changes for the caller; with more than one
  the request must include `stream_id` or gets a 409 listing the candidates, rather
  than silently guessing on the caller's behalf. Verified by 6 assertions against
  SQLite (`test_schema.py`, including that a landed flight is excluded) and 6 more
  end-to-end against a real db-writer and PostgreSQL (`run_mediamtx_auth.sh`,
  22/22 total): the one-flight case still just works, a second concurrent flight
  makes the plain request 409 rather than pick one, `stream_id` resolves each flight
  correctly, and a user cannot use `stream_id` to reach another tenant's flight.

Distinct from the section below: these are live weaknesses on this branch right now, not
work that has yet to start.

- **`danger_detection` mode has never run end to end** — and the blocker is not what
  this section previously claimed. It said the mode needed a TensorRT `.engine` built
  for the target GPU and that none existed. Both halves were wrong: `engine/` holds
  `segmentation_1280_720.engine` and `best_model_segunified_1280_720.engine`, and
  `segmentation.py` dispatches on the checkpoint's extension — `.engine` loads through
  `_TrtSession`, anything else through an onnxruntime CUDA session — so the ONNX path
  that `configs/danger_detection/segmenter.yaml` actually points at runs without any
  engine at all. `device="cuda"` is hardcoded, but that only means "needs a GPU", which
  the real-app harness already provides via `--gpus all`.

  What actually blocks it: `run_orchestrator_real_app.sh` hardcodes
  `APP_ENV_APP_MODE=health_monitoring`, so nothing has ever driven the other mode.
  **This is also why the telemetry plane is unverified against the real app.**
  `danger_detection` is the only mode that consumes telemetry — `health_monitoring`
  builds no `FrameTelemetryCombiner` — so §4's Mosquitto work, the
  `TELEMETRY_LISTENER_STREAM_KEY` the orchestrator injects, and the app's reuse of its
  publisher token as an MQTT username have been exercised only by synthetic test
  clients in `run_mqtt_auth.sh`, never by the container that will really do it.
  Parameterising the runner and giving the harness a telemetry publisher closes the
  second mode and the last unexercised plane together.
- **The orchestrator holds the Docker socket.** Anything that can reach its port can
  start containers on the host. Its port is internal-only, but this is the strongest
  argument for the Kubernetes backend, where the equivalent is a scoped service account.

### Built but not yet wired to anything

The schema and its accessors exist and are tested; **no service consumes them yet**, so
they change nothing about how the system currently behaves.

- `UserDirectory.create_stream` / `list_streams` / `revoke_stream` / `rotate_stream_key`,
  with cross-user access refused — these are the portal's operations, and there is no
  portal
- `db_writer/rebuild_schema.py` — destructive drop/create plus optional seeding

`streams`, `stream_key`, `flights.stream_id`, `flights.public_uuid`,
`generate_stream_key()` and `resolve_stream_key` are no longer in this list: the
MediaMTX auth hook is their first real consumer. `flights.output_path` also drops off
this list — it is set inside `open_flight_for_key` the moment a flight opens, and
`/flight/open` returns it directly to the orchestrator.

### Designed, not built

- Kubernetes `FlightRuntime` backend (create Job, delete Job)

### Open

- Portal (registration, stream slot CRUD, key rotation, viewer token issuance)
- Recorder per-tenant upload prefixes
- TLS certificate issue and renewal
- **Auth-endpoint caching and db-writer replica count.** Every publish and every read
  now costs one indexed lookup here. A short-TTL cache is the obvious fix and the wrong
  one to reach for blindly: it delays revocation of a credential that has no expiry.
  Replicas first, cache only if measurement demands it.
