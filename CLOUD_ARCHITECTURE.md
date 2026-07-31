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

MediaMTX, Mosquitto, ws-server, db-writer, Redis, the recorder and the portal are
**shared, multi-tenant services**. One deployment serves every user. Replicas are added
when load demands it, not when a user signs up.

These are I/O-bound and cheap. Running a private copy per user would waste an order of
magnitude of resource and multiply the operational surface (certificates, DNS entries,
health checks, upgrades) by the user count.

#### Data plane and control plane

The hub divides again, and the split is worth naming because it decides what is worth
being woken up for:

```text
data plane     in the path of every frame        MediaMTX, Mosquitto, ws-server,
                                                 Redis, recorder
control plane  in the path of decisions          db-writer, orchestrator, portal
```

**Nothing stops flying when the control plane is down.** Drones keep publishing,
containers keep processing, recordings keep uploading, and a viewer already holding a
token keeps watching. What breaks is signing up, adding a stream, opening or closing a
flight, and issuing a *new* viewer token.

db-writer is on both lists, and that is the whole reason its replica count is an open
question (§9): `/auth/mediamtx` sits on the critical path of every publish and every
read, while `/streams` does not. Sizing follows the auth endpoint; the portal's routes
ride along on capacity that already had to exist.

Redis now serves both planes too — alert fan-out for ws-server, rate-limit counters for
the portal (§4) — on separate logical databases. Neither use is authoritative for
anything: losing the whole instance drops in-flight fan-out and resets some counters, and
the portal is built to keep signing people in through exactly that.

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
                  │                            │
  account  ──────▶│  portal     (N replicas)   │      one per ACTIVE flight,
  holders         └────────────────────────────┘      created when the stream
                      replicas follow LOAD,           starts, destroyed when
                      never user count                it stops
```

One MediaMTX handles all four publishers. One Mosquitto handles all four telemetry feeds.
Only the GPU tier multiplies with flights.

The portal sits in the hub rather than the app tier, and the reason is sharper than
"it is I/O-bound". The app tier's entire economic argument is that it **scales to zero
when nothing is flying** — but that is exactly when people register, add stream slots and
rotate keys. A drone flies at 10am; the account was created at 11pm the night before.
An app-tier portal would exist only while a drone was airborne, which inverts its purpose.

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

Kubernetes does **not** reverse-proxy anything itself: `Ingress` and the Gateway API are
interfaces, and a controller has to be installed to implement them. That controller is
Traefik here, and it is the same Traefik the compose stack will run, which is why the
routing config survives the migration rather than being rewritten into a vendor's
annotations. Nothing in the design depends on Traefik specifically — see §7.

Managed Kubernetes also brings cert-manager, which is what closes the TLS item in §9 for
all three terminators at once: Traefik for the HTTP family, and Secrets mounted by
MediaMTX and Mosquitto for the protocols they terminate themselves.

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

Four credential types, one per class of client. They differ because the constraints on
the party presenting them differ — this is deliberate, not inconsistency.

| Channel | Credential | Lifetime | Why this form |
| --- | --- | --- | --- |
| Publisher → MediaMTX | **Stream key** | Until revoked | Typed by hand into a controller before each flight; must be short |
| Browser → WebRTC / HLS / WebSocket | **JWT** | Hours | Carried by software; length is free, expiry is free |
| App container → hub | **Injected token** | Container lifetime | Never touched by a human |
| Browser → portal | **Session token** | Hours | Stands in for a password across many clicks, so the password is presented once and never stored |

The first three all answer a question about a **thing** — this drone may publish here,
this container may write to flight 7, this browser tab may watch flight 7. None of them
answers *"this person owns account 3"*, which is the only question the portal ever asks.
That gap is why a fourth type exists rather than reusing the viewer token.

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

A short-lived JWT (HS256), minted by db-writer, naming exactly one `flight_id`. Obtained
from `POST /viewer/token` by presenting a **session token** — a deliberate credential
downgrade, and the shape worth noting: the caller offers something that identifies their
whole account and receives something that can watch one flight and do nothing else, with
no path back. A viewer token cannot mint another viewer token, and neither can a
publisher token; both are refused by the scope check, which matters because a viewer
token that could renew itself would never expire in practice.

ws-server validates it offline — signature and expiry only, no database or network call —
so any replica can authorise any viewer. The token travels in the WebSocket query string, because
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

### Session tokens **[built]**

The portal's credential, and the third value of the same `scope` claim: `session`. Where
a viewer token says *"bearer may watch flight 7"*, a session token says *"bearer is user
3"* — one field different, the same HS256 signature over the same `SESSION_JWT_SECRET`,
validated offline by any replica exactly as viewer tokens already are. **No new
infrastructure; one new scope value.**

The password is presented once at login, exchanged for this token, and never seen again.
That is not a stylistic preference. `/viewer/token` used to take email and password in
the body of every request, which is fine for a one-shot call and unusable for a portal,
where a user clicks around for twenty minutes and the page silently refreshes a viewer
token whenever one expires: the password would have to be kept somewhere for the whole
session. §6 already tells this story about the app container — it used to carry the
operator's email and password, and the fix was to inject a scoped token instead.
Building the portal on the `/viewer/token` pattern would have put the same reusable
password back, this time in the one tier that faces the public internet.

**`POST /login` is now the only route in the system that accepts a password.**

**The `user_id` must come from the token, never from the URL.**
`UserDirectory.revoke_stream(stream_id, user_id)` already refuses cross-user access, but
that check is only worth anything if `user_id` is trustworthy. Taken from a path
parameter it is a guess; taken from a signed claim it is a fact. This is the same rule
the viewer token follows: *no identifier in this system is ever a credential.*

The two browser-held credentials get deliberately different exposure, because they are
worth different amounts:

| Token | Where it lives | Why |
| --- | --- | --- |
| **Session** | `httpOnly` cookie | Browser JavaScript cannot read it, so an XSS bug in the front-end cannot steal the credential that controls the whole account |
| **Viewer** | readable by JS | It *must* be — JS puts it in a WebSocket query string and an `Authorization` header. Flight-scoped and hours-long, so it is designed to be exposed |

Two structural details, both load-bearing:

**A session token carries no `flight_id` claim at all** — not a null, not a zero. The
two flight-scoped kinds answer "which flight"; this one answers "which user". Omitting
the claim means a session token cannot satisfy `flight_id_from_credential` even if some
future caller forgets to check the scope, which makes the separation structural rather
than a check somebody has to remember. Verified in both directions, including against
ws-server, which rejects a session token as either of its two kinds.

**The scope check is what stops a viewer token being an account credential.** Viewer
tokens carry a `sub` claim naming their user — they always have, for logging — so the
scope claim is the *only* difference between "may watch flight 7" and "is user 3". This
is the same escalation §3 already describes between view and publish, and it is the
reason the third scope was added to the existing mechanism rather than a new one being
invented alongside it.

It is the shortest-lived of the three (8 h, against the viewer token's 12) because it is
the most powerful and there is no refresh: it is the whole session.

### Registration is open **[built]**

Anyone may create an account. The consequence to keep in view: an account can mint stream
keys, and a stream key is the thing that causes a GPU container to be created. Open
registration therefore connects an anonymous signup to GPU spend, and the limit on that
is **concurrent flights per user**, which nothing enforces yet — see §9.

`UserDirectory.create_user` is the only way an account comes into existence, including
from `rebuild_schema.py --seed-user`, which used to build the rows itself. A seeded
account the portal would have refused to create is a fixture that does not represent a
real user, and the bug that hides only appears in production.

Three properties of it are worth stating because each closes a defect that is invisible
until it bites:

- **Emails are normalised on write *and* on read.** PostgreSQL's unique constraint is
  case-sensitive, so `Alice@example.com` and `alice@example.com` are two accounts —
  and whichever casing the user did not type at login fails to authenticate with
  nothing on screen to explain it. Normalising on write alone would not fix that;
  `authenticate` had to change too.
- **Duplicates are caught by the constraint, not by a prior `SELECT`.** db-writer runs
  N replicas, so check-then-insert is a race two simultaneous registrations of the same
  address can both pass. The unique index is the only arbiter that sees both.
  `EmailAlreadyRegistered` subclasses `ValueError`, so the HTTP layer can answer 409
  rather than 400 without matching on message text.
- **Passwords are bounded at 72 *bytes*.** That is bcrypt's limit, not a policy: every
  byte past it is ignored by the algorithm, and bcrypt 5.x raises rather than truncating,
  so an unchecked long passphrase is a 500. Bytes rather than characters, because one
  emoji is four of them. The minimum is 8 (NIST SP 800-63B) with no composition rules,
  which the same document advises against.

---

## 4. Component tenancy

> **Read the "Instances" column first.** Everything below except the GPU app is a
> **single shared deployment** serving all users at once. Phrases like "per-flight JWT"
> or "channel per flight" describe how one shared service *separates tenants internally* —
> they do **not** mean a copy of that service exists per flight. Exactly one row in this
> table is instanced per flight.

| Component | Instances | Tenancy mechanism | State |
| --- | --- | --- | --- |
| **GPU app** | **One per active flight** | Sole occupant — no internal tenancy needed | container **[built]**, lifecycle **[built]**, paths **[verified]** against a real GPU in **both** modes |
| MediaMTX | Shared, replicated on load | Regex paths + HTTP auth hook | **[built]** |
| Mosquitto | Shared, replicated on load | Per-stream credentials + topic ACLs | **[built]** |
| ws-server | Shared, replicated on load | Per-flight JWT (view + publish scopes); Redis pub/sub fan-out | **[built]** |
| db-writer | Shared, replicated on load | Stateless per request; bcrypt user auth | **[built]** |
| Redis | Shared | Channel per flight (`flight:{id}`); rate-limit counters on db 1 | **[built]** |
| Recorder | Shared | Segment → flight_id resolved via `recordings` table | upload **[built]**, per-tenant prefix **[open]** |
| Orchestrator | Shared | Spawns/stops app containers | **[built]** |
| Portal | Shared, replicated on load | Session token → `user_id`, read from the claim not the URL | **[built]** |

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

### Portal **[built]**

"The portal" is two things that land in different places, and conflating them is the
easiest way to get this wrong:

| Half | What it is | Where it goes |
| --- | --- | --- |
| **User API** — register, login, list/add/rotate/revoke streams, issue viewer token | HTTP over the `users` and `streams` tables | **db-writer.** New routes on an existing service, not a new one |
| **Web front-end** — the pages a human clicks | HTML/JS, holds the session cookie | **A new hub service** (`portal/`). This is the only genuinely new deployment |

The API half belongs in db-writer because db-writer already owns that schema.
`UserDirectory.create_stream` / `list_streams` / `revoke_stream` / `rotate_stream_key`
are already there and already refuse cross-user access; they have simply never had an
HTTP route. A second service writing the same tables would give two authorities over one
schema with nothing to say which is right — the objection §5 uses to keep `user_id` off
the `flights` table, applied to services instead of columns.

That API now exists on db-writer: `POST /register`, `POST /login`, `GET /me`, and
`GET/POST /streams` with `POST /streams/{id}/rotate` and `/revoke`. Every account-scoped
route takes `user_id` from the session claim through `_require_session`, never from the
URL or the body — which is what makes `UserDirectory`'s existing cross-user refusals
worth anything, since they only hold if the `user_id` handed to them is a fact rather
than a guess. A `stream_id` naming another user's slot is answered with the **same 404**
as one that does not exist; `stream_id` is sequential, so distinguishing them would
confirm the existence of a row the caller has no business knowing about.

`POST /viewer/token` is authorised the same way — by the session token, not by a
password (§3) — so `POST /login` is the only route in the system that takes one.

`GET /flights` completes the set: the caller's currently airborne flights, which is what
the dashboard marks *live* and what decides whether a Watch button exists at all. It
returns exactly what `/viewer/token` disambiguates over, deliberately — the page that
offers the button and the call that authorises pressing it must agree on what is active,
and two different queries would eventually disagree.

#### The front-end

Server-rendered HTML from a small FastAPI service, no build step and no framework. The
whole surface is four pages — sign in, register, slots, watch — and a toolchain would be
more moving parts than the pages themselves.

| Page | What it does |
| --- | --- |
| `/login`, `/register` | The only forms that carry a password. Registration signs the new account straight in |
| `/` | Slots, each with the full `rtmps://` ingest URL to retype, plus New key / Retire, and a Watch button on whatever is live |
| `/watch` | The annotated video and the alert feed for one flight |

Three things about it are load-bearing rather than incidental:

- **The session token never reaches the page.** It lives in an `httpOnly`, `Secure`,
  `SameSite=strict` cookie, and the rendered HTML is asserted not to contain it (§9).
  Printing it into the document would give back exactly what `httpOnly` was for.
- **The watch page holds only a downgrade.** It fetches a viewer token from the portal
  at load time rather than having one baked into the HTML, so the credential is never in
  a document a proxy or browser might cache. The portal composes the WebRTC, HLS and
  WebSocket URLs from the flight's `output_path` and the public hostnames — and is then
  not in the path of any of them: video is browser-to-MediaMTX end to end.
- **State-changing requests are checked twice.** `SameSite=strict` stops the browser
  attaching the cookie to a cross-site request, and an `Origin`/`Referer` check refuses
  it server-side. They fail independently, and what they guard is real: a cross-site
  POST to `/streams/{id}/revoke` would take a tenant's ingest key out of service.

Tenant-supplied text — slot labels, alert messages — is escaped on the way into the DOM
in both directions: Jinja autoescaping server-side, `textContent` rather than `innerHTML`
in the alert renderer. A label is the one field a tenant controls that the portal renders
back to them.

#### Rate limiting the two anonymous endpoints **[built]**

`/login` and `/register` are the only endpoints in the system that anyone on the internet
can reach without a credential — and they cannot be given one, since a sign-in form is
what a caller uses *before* it has anything. Counting is the only brake available, and
until it existed the sole limit on password guessing was bcrypt's own cost.

The counters live in **Redis, not process memory.** This is the same statelessness
argument the session makes, arriving at the opposite answer: a session can live in a
signed cookie because the client can be trusted to carry it, and a rate limit cannot,
because the client is the thing being limited. N replicas each holding their own counter
is a limit of N × whatever is written down.

| Endpoint | Counted per | Default | Counting |
| --- | --- | --- | --- |
| `/login` | account (hashed email) | 10 / 15 min | failures only; cleared by a success |
| `/login` | source address | 30 / 15 min | failures only; **not** cleared by a success |
| `/register` | source address | 20 / hour | every attempt, successful or not |

Every line of that table is a way the limit would otherwise be walked past:

- **Two counters on login, because neither bound implies the other.** Per-address alone
  is evaded by a botnet — a thousand hosts trying ten passwords each. Per-account alone
  is evaded by spraying one common password across a thousand accounts from one host.
- **Failures count, successes do not**, so a busy legitimate user is never locked out by
  their own activity. A success clears the *account's* counter and deliberately leaves
  the address's: an attacker who holds one valid account would otherwise reset their own
  budget whenever they liked.
- **The account key is the normalised address**, hashed. Normalised because otherwise
  `Alice@` and `alice@` are two buckets for one account and the limit is bypassed by
  pressing shift; hashed because those keys are the only place the portal would hold a
  list of user email addresses, and it has no reason to hold one.
- **Registration counts attempts, not accounts.** A 409 on a taken address is an
  account-existence oracle whether or not a row is created.

The check happens **before** db-writer is called, so an over-limit attempt costs no bcrypt
verification — otherwise the limiter becomes the cheapest known way to load the database
with expensive work.

##### Which address is "the source"

`X-Forwarded-For` is appended to by each proxy, so the client is the entry
`TRUSTED_PROXY_HOPS` from the **right**; everything further left was supplied by the
client and is forgeable. Taking the leftmost entry — the common shortcut — lets a client
name its own bucket, and that is not merely evasion: it lets one client push another
client's bucket to the limit and lock them out. The default is 0, which trusts nothing and
uses the peer address, which is the compose stack today; each HTTP proxy actually in the
path adds one (§8). **Setting it higher than the truth is the dangerous direction**, and
both configurations are tested against each other (§9).

##### It fails open

If Redis cannot be reached the request is allowed and the failure logged. A rate limiter
that turns a Redis outage into "nobody can sign in" has become a worse outage than the
attack it prevents. `REDIS_URL` is nonetheless *required* at startup — an unreachable
Redis is an incident, while an unset variable is a portal that was never rate limited at
all and never said so.

Fixed windows, not sliding, and check-then-increment rather than a lock: a burst can
exceed the limit slightly at a window boundary or under concurrency. Both are accepted,
because the purpose here is to turn an unbounded guessing rate into a bounded one and 21
attempts instead of 20 changes nothing. Contrast the stream cap in §4, which *does* take a
row lock — there an overshoot is a GPU container somebody pays for.

#### Adding a stream is the endpoint that spends money

`POST /streams` is where an account becomes GPU capacity — a slot is what lets a
container come into existence — and registration is open, so it is capped at
`MAX_STREAMS_PER_USER` **active** slots per user. Retired slots do not count, because a
retired slot cannot publish.

Two things make that cap real rather than advisory:

- **Rotation is capped too.** Rotating a retired slot revives it, which is how a user
  brings one back — so without the same check, revoke → add → rotate would net one slot
  over the limit on every repeat.
- **The count is taken under a row lock on the owning user.** Unlike the duplicate-email
  case there is no unique constraint to catch an overshoot afterwards, so with N replicas
  a plain count-then-insert is advisory only. Verified rather than assumed: 20 simultaneous
  adds across two replicas create exactly 10 slots, and with the lock removed the same
  test creates 11.

#### The browser never reaches db-writer

§8 requires db-writer to be unroutable from outside. The portal is what preserves that:

```text
browser ──HTTPS──▶ portal ──private network──▶ db-writer
        session cookie          internal HTTP
```

The portal is the only new thing on the public internet; db-writer's exposure is
unchanged. This is also what makes the `httpOnly` session cookie possible — a token the
browser holds but cannot read is only useful if something server-side reads it, and that
something is the portal.

#### It must be stateless, for a reason already learned once

The session lives in the signed cookie, **not in portal memory**. Any replica can then
serve any request, because everything it needs arrived with the request.

Note what the portal does *not* do with that cookie: validate it. It cannot — it does not
hold `SESSION_JWT_SECRET` (§7) — so it forwards the value and treats db-writer's 401 as
the answer. That is one extra hop per request, and it buys the signing key of every
credential in the system being absent from the tier facing the internet. Verified rather
than asserted: the replicas in `run_portal.sh` are started with no `SESSION_JWT_SECRET`
and no database variables at all, and serve every page in the test.

The failure mode being avoided is one this system has already hit: in-memory sessions
mean user 3's session exists on replica 1 and replica 2 has never heard of them — which
is precisely the defect §4 describes in ws-server, "horizontal replicas cannot share an
in-memory client set". That one needed Redis to fix. This one needs nothing, because a
signed cookie carries its own state, and **it stays that way only if no server-side
session store is ever introduced.**

Load will be the lowest in the hub — clicks, not frames — so replicas are about not
being a single point of failure rather than capacity.

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
| `streams` | `stream_id` PK, `user_id` FK, `stream_key` unique, `label`, `revoked_at` | **[built]**, capped per user (§4) |
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

**Every step is now [built], end to end.** Step 6 is verified against a real GPU in both
modes. What no test reaches is the viewer's browser at the end of it — see §9.

1. User registers on the portal → row in `users`, and logs in for a session token.
   Registration is open to anyone. **[built]** — the portal's `/register` and `/login`
   pages over db-writer's routes of the same names.
2. User adds a stream → row in `streams` with a generated `stream_key`. The portal
   offers rotate and retire. **[built]** — `GET/POST /streams`, `/rotate`, `/revoke`
   on db-writer, capped per user (§4), driven from the slots page.
   The key is *not* shown once and then hidden: `list_streams` returns it every time,
   because the operator has to retype the ingest URL before every flight.
3. Operator types `rtmps://ingest.<host>:1936/in/<key>` into the controller.
4. Publisher connects. MediaMTX POSTs `{action: "publish", path: "in/<key>"}` to db-writer,
   which resolves the key and checks `revoked_at IS NULL` → 200.
5. Stream goes live. `runOnAvailable` fires with `$G1` = the key. The orchestrator calls
   db-writer's `/flight/open`, which **creates the flight row** and mints a publisher
   token, then spawns the container with `flight_id`, ingest path, output path and token
   injected as environment.
6. App reads `in/<key>`, publishes annotated video to `out/<public_uuid>`, POSTs alerts to
   ws-server and db-writer. **[verified against a real GPU container in both modes — see
   §9. `danger_detection` additionally consumes live telemetry over Mosquitto, which
   `health_monitoring` never does]**
7. Viewer opens the portal and — holding a session token from step 1 — calls
   `POST /viewer/token`, receiving a JWT scoped to that one flight, which it presents for
   the WebRTC/HLS read and the WebSocket connection alike. MediaMTX validates it through
   the same auth endpoint with `action: "read"`. **[built]** — the watch page fetches it
   at load; with more than one flight active the call must name a `stream_id` rather than
   being guessed for.
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
| Browser → portal | Public internet | HTTPS; session token in an `httpOnly` cookie |
| Portal → db-writer | Private | Internal HTTP; the browser never crosses this hop |
| App ↔ hub | Cloud virtual network | Not separately encrypted — see below |
| Hub → database | Private | Worker credentials, least privilege |

The four public rows describe the target. The transport half of each — RTMPS, MQTTS,
HTTPS/WSS — is **not deployed today**; the credential half (stream keys, per-stream MQTT
users and ACLs, per-flight JWTs, the session cookie) is built and tested. So every public
credential in the system currently crosses the internet in the clear, which is the single
largest gap between this section and the running stack.

### TLS termination **[designed]**

Two layers, split by protocol.

**A cloud L4 load balancer is the edge** — NLB, Azure Standard LB, GCP network LB — and
deliberately *not* a cloud L7 load balancer. ALB, Application Gateway and the GCP HTTPS LB
carry HTTP and nothing else, but most of what reaches this hub is not HTTP: RTMPS and RTSPS
are arbitrary TCP, MQTTS is arbitrary TCP, and WebRTC media is UDP. A cloud L7 load balancer
cannot front this system at all, so picking one would mean standing up a second L4 edge
beside it — two edges to avoid one pod, which is worse than either choice made cleanly. The
L4 layer stays managed, because terminating raw TCP in a pod we operate buys nothing: the
provider's LB sits in the cloud fabric, outlives the cluster, and carries its DDoS
protection with it.

**Traefik is the L7 proxy behind it**, in-cluster, terminating HTTPS and WSS for the
HTTP-family services only — HLS, WHEP signalling, and the portal. It stays in-cluster
rather than dissolving into load-balancer configuration for three reasons:

- the routing config is then the same under docker-compose and under Kubernetes, and the
  compose deployment does not go away — the interim laptop-app deployment below still needs
  an answer, and a cloud LB gives nothing locally
- the middlewares are ours rather than a vendor's annotation dialect
- nothing about it is provider-specific, which is the posture the recorder already takes
  with `local | azure | aws`

Traefik is not load-bearing in that choice. Caddy or ingress-nginx would fill the same role;
the slice is small and the decision is reversible, which is the right size for it.

**MediaMTX and Mosquitto terminate their own TLS**, for two reasons: it preserves the client
identity that ACLs and the auth hook depend on, and WebRTC media is DTLS-SRTP over UDP
end-to-end, so it bypasses an L7 proxy entirely. Only WHEP signalling is HTTP.

(Traefik *does* support TCP routers with SNI, so proxying RTMPS is possible — self-termination
is chosen for the identity-preservation reason, not because of a Traefik limitation.)

**Certificates come from cert-manager, not from the load balancer**, and this is the reverse
of the usual argument. A cloud-managed certificate terminates *at* the LB and hands back no
key file — but MediaMTX and Mosquitto self-terminate and need a certificate and key on disk.
A managed certificate therefore covers only the one slice that could have gone either way,
leaving cert-manager to be run anyway for the other two: two issuance mechanisms where one
would do. cert-manager writing into Secrets that all three services mount is that one
mechanism.

**Nothing in this subsection exists yet.** There is no Traefik service in
`docker-compose.yml`, `configs/mediamtx/mediamtx.yaml` configures no encryption on any
listener, and Mosquitto's MQTTS block is commented out. Every public-facing protocol in the
running stack is currently in the clear, and the portal's own `PORTAL_COOKIE_SECURE` /
`PORTAL_PUBLIC_TLS` defaults assume a terminator that has not been deployed. See §9.

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

**The portal does not get this secret.** It mints nothing and validates nothing: it holds
the session cookie, forwards its value to db-writer, and lets db-writer answer 401 if the
token is bad. That keeps the signing key of every credential in the system out of the one
tier facing the public internet — worth the extra hop, since a portal that validated
locally would have to hold the secret that also signs publisher tokens.

There is **no pre-shared publisher secret**. App containers receive a token scoped to
their own flight when the flight opens, so no long-lived credential is distributed to the
GPU tier at all.

---

## 8. Network topology

Externally reachable. The **Today** column is what `docker-compose.yml` actually publishes,
which is not yet the target — see the TLS item in §9.

| Port | Protocol | Terminated by | Today |
| --- | --- | --- | --- |
| 1935 | RTMP (fallback only) | MediaMTX | published, in the clear |
| 1936 | RTMPS | MediaMTX | **not configured** |
| 8554 | RTSP (fallback only) | MediaMTX | published, in the clear |
| 8322 | RTSPS | MediaMTX | **not configured** |
| 8888 | HLS | Traefik | published direct, plain HTTP |
| 8889 | WebRTC / WHEP signalling | Traefik | published direct, plain HTTP |
| 8189/udp | WebRTC media | End-to-end DTLS-SRTP — **must not be proxied** | published |
| 1883 | MQTT (fallback only) | Mosquitto | published, in the clear |
| 8883 | MQTTS | Mosquitto | **commented out** in `mosquitto.conf` |
| 443 | HTTPS + WSS — includes the portal | Traefik | **no Traefik service exists** |

The plain-text ports are labelled *fallback only* because that is their designed role —
drones without TLS support (§7). Today they are not a fallback but the only path, since no
encrypted listener is configured on any of the three.

In compose the portal is published on **8003** and speaks plain HTTP; 443 and the
certificate are the ingress tier's in a real deployment. Two variables exist for the gap
between those worlds, `PORTAL_COOKIE_SECURE` and `PORTAL_PUBLIC_TLS`, and both default to
on. Turning them off is a local-HTTP affordance and nothing else: a `Secure` cookie is
simply not returned over `http://`, so the symptom of leaving one off in production is not
an error but a login that appears to work and then forgets the user. The corollary is that
with the defaults on and no terminator deployed, **the portal has no working configuration
today** — either the cookie never comes back, or it runs with the local-only affordance
enabled in production.

`PORTAL_TRUSTED_PROXY_HOPS` must count the *real* hops between the browser and the portal,
which is a deployment fact and not a property of any product name. An L4 load balancer with
source-IP preservation adds none, so a browser → LB → Traefik → portal path is 1; a cloud
L7 LB in front of Traefik would make it 2. Counting too high is the dangerous direction —
the client then names its own rate-limit bucket (§4).

Internal only — **must never be routed from outside**: ws-server's alert-write API port,
db-writer, Redis, the recorder, and the orchestrator.

The portal is the only *new* service on the public side, and it is what keeps db-writer
off it: the browser talks to the portal, the portal talks to db-writer over the private
network (§4). Routing db-writer's user-facing endpoints directly to the browser would be
simpler by one hop and would break this line.

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

- **Rate limiting on `/login` and `/register`** (§4). The two endpoints anyone on the
  internet can reach without a credential, and until now the only brake on guessing a
  password was bcrypt's own cost. Verified by 25 assertions inside `run_portal.sh`.

  Three of them were checked by breaking the thing they test, because a rate-limit
  assertion that passes for the wrong reason is worse than none:

  - **The counters are shared across replicas.** Confirmed non-vacuous by pointing the
    two replicas at different Redis databases — that assertion, and only that one, then
    fails. This is what an in-process counter would look like: a limit of N × the number
    written down.
  - **A forged `X-Forwarded-For` mints no fresh bucket** where no proxy is trusted, while
    the same header is believed by the replica configured for one hop. The two replicas
    run with different trust settings on purpose. Confirmed non-vacuous by setting both
    to trust one hop and watching the first half fail.
  - **The limiter fails open.** With Redis stopped, a correct sign-in still returns 303
    and a wrong one still returns 401 rather than 500 — asserted in the runner, since it
    has to stop a container.

  Also pinned: the per-account and per-address limits are genuinely separate in both
  directions (one locked-out account does not lock out its neighbours on the same NAT;
  twelve *different* accounts sprayed from one address are still refused); a success
  clears the account's counter but not the address's; registration counts eight
  *malformed* attempts, since a 409 on a taken address is an existence oracle whether or
  not a row is created; and the 429 carries a `Retry-After` a client can obey alongside a
  wait a person can read.
- **The portal front-end** (§4). The service in `portal/`, and with it the last piece of
  the flight lifecycle: every step in §6 now has something that calls it. Verified by 69
  assertions in `run_portal.sh`, driving it the way a browser does — form posts, a session
  cookie, an `Origin` header — against a real db-writer and real PostgreSQL, on **two
  portal replicas**.

  Three of those assertions are the ones worth naming, because each pins a claim made
  elsewhere in this document:

  - **The rendered HTML never contains the session token.** Searched for on the dashboard
    and the watch page. `httpOnly` buys nothing if the token is also printed into the
    document it protects.
  - **What the watch page receives cannot act as a session.** The token in the video URL
    is checked to differ from the cookie's, then spent against db-writer and refused —
    both as a session and as a request for another viewer token. The downgrade in §3 with
    no path back, tested from the far end.
  - **The replicas hold no secret and no database credentials.** They are started with
    neither `SESSION_JWT_SECRET` nor any `DB_*` variable and still serve every page, which
    is how §7's claim is falsifiable rather than merely stated.

  Also pinned: a cookie issued by one replica is accepted by the other (statelessness,
  the property this design keeps re-earning); cross-site POSTs to add, revoke and login
  are refused, as is one carrying neither `Origin` nor `Referer`, and the refusals are
  shown to have created nothing; a slot labelled `<script>alert(1)</script>` comes back
  escaped; and a rotated key vanishes from the page in the same request that replaces it.

  Not covered: whether video actually plays. That is WebRTC between the browser and
  MediaMTX, and the portal composes a URL rather than sitting in the path — the URL's
  shape is asserted, the picture is not.
- **`/viewer/token` takes a session token, not a password** (§3). The last route outside
  `/login` that accepted one, so a password now reaches exactly one endpoint in the whole
  system. Verified inside `run_mediamtx_auth.sh` (27/27, up from 22): email and password
  are refused where they used to work, no credential is refused, and — the two that
  matter — **a viewer token cannot mint another viewer token** and neither can a
  publisher token. The first would be a self-renewing credential that never expires in
  practice; the second is held inside a container processing untrusted video. A second
  tenant's session token still cannot reach the first's flight even when naming its
  `stream_id`. The disambiguation behaviour is unchanged and still verified: one active
  flight resolves silently, two force a 409 rather than a guess.
- **Stream slot CRUD — `GET/POST /streams`, `/rotate`, `/revoke`** (§4). The portal's
  operations, finally reachable, each scoped by the session claim rather than by
  anything in the request. Verified by 22 assertions in `test_schema.py` on the cap and
  27 more end-to-end in `run_portal_auth.sh` (49 total there): every route 401s without
  a token and with a garbage one; another tenant gets the **same 404** for a stream that
  is not theirs as for one that does not exist, and their failed rotate leaves the
  owner's key untouched; a rotated key is what the *other* replica then reports; revoke
  hides the slot while `include_revoked=true` still shows it with `revoked_at` set and
  nothing deleted. The cap is verified three ways — sequentially, against the
  revoke → add → rotate revival bypass, and under **20 simultaneous adds across two
  replicas**, which create exactly 10. That last one was confirmed non-vacuous by
  removing the row lock and watching it create 11.
- **Portal authentication — `/register`, `/login`, `/me`** (§3 session tokens). The
  third scope on the existing JWT mechanism, so no new infrastructure. Verified by 21
  assertions on the token itself (`test_session_tokens.py`) and 20 end-to-end over real
  HTTP against real PostgreSQL with **two replicas** (`run_portal_auth.sh`). The two
  that matter most: a **viewer token is refused as a session token** — it carries a
  `sub` claim naming its user, so the scope check is the only thing between "may watch
  flight 7" and "is user 3" — and a **token minted on replica 1 is accepted on replica
  2**, which is the stateless property the whole design rests on and what an in-memory
  session store would silently break. Also pinned: a session token carries no
  `flight_id` claim, so it is refused by `flight_id_from_credential` and by ws-server
  in both of ws-server's scopes; forged signatures, expiry, `alg:none`, missing and
  non-numeric subjects, five malformed `Authorization` headers; and the status codes
  the portal will branch on (409 duplicate, 400 malformed, 401 bad credentials) with a
  failed login never disclosing whether the account exists.
- **Account registration** (`UserDirectory.create_user`, §3). Verified by 22 assertions
  against SQLite in `test_schema.py` — email normalisation on both write and read,
  duplicate refusal including a differing case and surrounding whitespace, the bcrypt
  72-**byte** boundary measured in bytes rather than characters, the 8-character floor,
  malformed and over-long addresses, and that a rejected registration leaves no row —
  plus 6 against **real PostgreSQL**, which is where the duplicate path actually
  matters: psycopg2's `IntegrityError` (not SQLite's) is what the race guard catches,
  the case-variant duplicate is refused by a genuinely case-sensitive unique index, and
  the directory keeps working after a rejected insert rather than being poisoned by it.
  `rebuild_schema.py --seed-user` was rewritten onto the same path and re-verified end
  to end, including that it now refuses to seed an account the portal would reject.
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
- **The real app tier, driven by the orchestrator, in BOTH modes.**
  `run_orchestrator_real_app.sh` builds the actual GPU app image (not the sleeping stub)
  and runs it with `--gpus all` behind the orchestrator: a live publish spawns it with
  the injected `FLIGHT_ID`/`PUBLISHER_TOKEN`/paths, it reads `in/<key>`, runs the full
  pipeline on the GPU with zero `CRITICAL` log lines, publishes annotated video to its
  own `out/<uuid>` (confirmed by MediaMTX's own "is publishing to path" log line), and is
  torn down cleanly on landing with the flight row closed. `danger_detection` 15/15,
  `health_monitoring` 9/9, real MediaMTX/Mosquitto/db-writer/ws-server/Redis/Postgres
  throughout. No alert was expected or produced in either: the input is an ffmpeg
  `testsrc` pattern with nothing in it to flag.

  The mode is now the runner's first argument and **defaults to `danger_detection`**. It
  was hardcoded to `health_monitoring`, which is the whole reason the primary product mode
  had never executed once. Three things had to be true before that could be fixed, and
  only the first was known:

  - **The TensorRT claim was wrong twice over.** This document already corrected the
    first half; the run settles it. `danger_detection_stream.py` resolves each model by
    looking for an `engine/<stem>.engine` and falling back to the `.pt` detector and
    `.onnx` segmenter named in `configs/danger_detection/*.yaml`. That fallback is the
    path the test takes. An engine is an accelerator, never a prerequisite.
  - **Nothing the workers log was observable.** Fifteen modules ended their logger setup
    with a hardcoded `setLevel(logging.WARNING)`, and `app/main.py` installs no
    `StreamHandler` at all — so `docker logs` on a flight container is near-empty and the
    old `docker logs | grep -c CRITICAL` assertion could not fail under any circumstance.
    Assertions now read `/app/logs/*.log` copied out of the live container, and the level
    comes from `LOG_LEVEL` (default `WARNING`, so production is unchanged; the harness
    sets `INFO`). An operator could not previously turn on the diagnostics the workers
    were already writing.
  - **The telemetry plane needed a real publisher.** See below.
- **The telemetry plane carries a real message from the real app.** `run_mqtt_auth.sh`
  proves Mosquitto's authorisation — who may publish where, and that one flight cannot
  read another's topics — but it proves nothing about the plane as a *pipe*, because no
  app is listening at the other end. `danger_detection` is the only mode that consumes
  telemetry (`health_monitoring` instantiates no `FrameTelemetryCombiner`), so until this
  run, §4's Mosquitto work, the `TELEMETRY_LISTENER_STREAM_KEY` the orchestrator injects,
  and the app's reuse of its publisher token as an MQTT username had been exercised only
  by synthetic clients.

  The harness now starts the broker with the **real `mosquitto.conf` against the real
  db-writer ACL endpoint**, plus `tests/comms/telemetry_publisher.py` authenticating as
  the drone with its stream key — two different credentials admitted to the same topics
  from opposite directions, which is the arrangement §3 describes.

  **The load-bearing assertion is that telemetry reaches the combiner, and it was checked
  by breaking it.** `FRAMETELCOMB_MAX_TIME_DIFF` is 150 ms, so a publisher can connect,
  authenticate, be admitted, and deliver every message on time — and still leave every
  frame unmatched if it publishes below ~7 Hz. Dropping the publisher to 1 Hz leaves all
  fourteen other assertions green and fails only this one, with 192 starved matches in the
  last 200 log lines. That is the failure an authorisation test cannot see: a plane that
  is correctly secured and carries nothing.
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
  27/27 total): the one-flight case still just works, a second concurrent flight
  makes the plain request 409 rather than pick one, `stream_id` resolves each flight
  correctly, and a user cannot use `stream_id` to reach another tenant's flight.

Distinct from the section below: these are live weaknesses on this branch right now, not
work that has yet to start.

- **The DEM is absent, so part of the geo stage is untested.** `dem/dem.tif` and
  `dem/dem_mask.tif` are gitignored and not on any machine here, and `open_dem_tifs()`
  returns `None` for a missing raster, so `danger_detection` runs with slope and no-data
  analysis skipped. Geofencing and the safety radius do run and are exercised. This is a
  supported degraded mode rather than a fault — the harness reports which of the two it
  got — but no test has yet driven a real elevation raster through `extract_dem_window`
  and the window cache.
- **The orchestrator holds the Docker socket.** Anything that can reach its port can
  start containers on the host. Its port is internal-only, but this is the strongest
  argument for the Kubernetes backend, where the equivalent is a scoped service account.

### Built but not yet wired to anything

The schema and its accessors exist and are tested; **no service consumes them yet**, so
they change nothing about how the system currently behaves.

- `db_writer/rebuild_schema.py` — destructive drop/create plus optional seeding

`streams`, `stream_key`, `flights.stream_id`, `flights.public_uuid`,
`generate_stream_key()` and `resolve_stream_key` are no longer in this list: the
MediaMTX auth hook is their first real consumer. `flights.output_path` also drops off
this list — it is set inside `open_flight_for_key` the moment a flight opens, and
`/flight/open` returns it directly to the orchestrator.

`UserDirectory.create_user` and the four stream-management methods have now dropped off
too: the routes in §4 are their first real consumer, which is what made this section's
list nearly empty. Everything here that mattered was portal work.

### Designed, not built

- Kubernetes `FlightRuntime` backend (create Job, delete Job)
- **The ingress tier — cloud L4 load balancer, Traefik, cert-manager (§7).** The shape is
  decided; none of the three pieces is deployed. Concretely missing: a `traefik` service in
  `docker-compose.yml` with routers for the portal, HLS and WHEP; `encryption` and cert
  paths on MediaMTX's RTMP/RTSP listeners; Mosquitto's commented-out MQTTS listener; and
  an issuer. Until it lands, every public protocol runs in the clear and the portal's own
  defaults describe a deployment that does not exist.

### Open

- **db-writer's own `/login` is still unrated.** The public door is now bounded (§4),
  but the endpoint behind it is not: anything that can reach db-writer directly can still
  guess passwords at bcrypt's pace. That is internal-only by §8 and so is not currently
  reachable, which is the whole reason this is a note rather than a hole — it becomes one
  the moment something else on the private network is compromised, or the day db-writer is
  routed anywhere it should not be.

  Still deliberately not addressed, at either layer: the response time of `/login`
  distinguishes "no such user" from "wrong password", because bcrypt runs only in the
  second case. `/register` discloses exactly the same fact outright by design, so a dummy
  hash on every failed login would cost real time and conceal nothing.
- **No email verification.** Registration accepts any syntactically valid address
  without proving the registrant controls it, so an account can be created against
  somebody else's address. Little is at stake while nothing is emailed — no password
  reset exists either — and both land together when one is needed.
- **A user may still fly their cap continuously.** `MAX_STREAMS_PER_USER` (§4) bounds
  how many flights one account can run *at once*, which was the open hole when open
  registration met `POST /streams`. What it does not bound is duration or total GPU
  hours: ten slots flying all day is within the cap. Quota and billing are the answer,
  and neither exists — this only becomes pressing once the Kubernetes node pool in §2
  can create machines on demand.
- **Signing out drops the cookie; it does not revoke the token.** A session token stays
  valid for its full eight hours whatever the user clicks, because there is no
  revocation list — that is the price of a stateless session and the reason the lifetime
  is hours rather than weeks. It covers logging out on a shared machine and does not
  cover a token already copied. A deny-list in Redis would fix it and would put a
  server-side lookup back on every request, which is the thing §4 is careful not to do;
  worth revisiting only if a real reason to force logout appears.
- **Browser playback is unverified.** `run_portal.sh` asserts the shape of the WebRTC and
  HLS URLs but no browser has opened one, and the watch page embeds MediaMTX's own reader
  page rather than negotiating WHEP itself — which assumes MediaMTX forwards the `?jwt=`
  query on to its WHEP request. That assumption is the one thing on the page that a test
  here cannot reach; it needs a human with a browser and a live flight. This is now the
  **only** remaining part of the end-to-end product path that has never been exercised —
  `danger_detection` and the telemetry plane came off this list below.
- **No flight history.** The portal shows what is airborne now and nothing that has
  landed, so recordings and past alerts are in the database and unreachable from the UI.
  Every row needed is already there (`flights`, `alerts`, `recordings`); what is missing
  is a paged read route and a page, and neither is on the path of anything else.
- Recorder per-tenant upload prefixes
- **TLS certificate issue and renewal.** Distinct from the ingress tier above, which is a
  deployment task: this is the ongoing one. cert-manager answers issue and renewal for
  Traefik directly, but MediaMTX and Mosquitto read a certificate from disk, so a renewed
  Secret has to reach a running process. Mosquitto rereads its certificates on `SIGHUP`;
  MediaMTX's behaviour on a changed cert file has not been checked, and if it does not
  reload, renewal means restarting the media server — which drops every flight in the air.
  Verify that before the first certificate expires rather than after.
- **Auth-endpoint caching and db-writer replica count.** Every publish and every read
  now costs one indexed lookup here. A short-TTL cache is the obvious fix and the wrong
  one to reach for blindly: it delays revocation of a credential that has no expiry.
  Replicas first, cache only if measurement demands it.
