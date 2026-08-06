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

One exception, stated here rather than discovered later: **MediaMTX does not replicate
behind a load balancer.** A path lives on the single instance its publisher connected to,
so adding replicas does not add capacity for an existing flight — it needs path-aware
routing or a relay tree instead. Everything else on this list is stateless or made so.
See §9; it is not pressing, because the GPU tier saturates long first.

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

### Platform: Kubernetes **[built]**

The target is **managed Kubernetes** (AKS/EKS/GKE — not self-hosted; self-managing etcd is
not where a small team should spend attention). The compose stack on a single host remains
the running deployment; the manifests for the whole hub tier now exist beside it in
`configs/k8s/hub/`, generated into ConfigMaps and applied by `kubectl apply -k configs/`,
and are deployed and asserted against a real cluster by
`tests/comms/run_hub_manifests.sh`. What is still missing is a cluster anyone is paying
for, and the provider-specific values that only exist once there is one.

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

#### Migration is mechanical, with two real changes **[built]**

Every hub service already has a Dockerfile and takes configuration from environment
variables, so compose services convert to Deployments directly. This section predicted
one genuine difference and there turned out to be two; the manifests are now written
and deployed on a real cluster, so both are findings rather than forecasts.

**The predicted one.** MediaMTX and Mosquitto need `LoadBalancer` services carrying TCP
and UDP, not an HTTP Ingress — which is the same split already chosen for TLS
termination in §7, so the topology and the security model agree. One correction to how
that was phrased: for MediaMTX it must be a **single** Service carrying both protocols,
not one of each. WebRTC advertises exactly one host candidate address
(`MTX_WEBRTCICEHOSTNAT1TO1IPS`), so 8189/tcp and 8189/udp have to arrive at the same
external address; two Services would allocate two, and the TCP half would fail for
precisely the viewers ICE-TCP exists to serve. Mixed-protocol load balancers are GA
from Kubernetes 1.26 and carried by Azure and AWS NLB; where a provider will not, the
fallback is two Services pinned to one pre-allocated address.

**The unpredicted one: the recorder cannot stay a separate Deployment.** It shares the
`recordings` volume with MediaMTX, and two pods sharing a volume needs `ReadWriteMany` —
Azure Files, EFS or Filestore. That is a paid, network-attached filesystem standing in
for what is a handoff between two processes that can sit on one node. So the recorder
becomes a **sidecar container in the MediaMTX pod**, sharing an ordinary
`ReadWriteOnce` claim.

It costs nothing, which is why it is the right answer rather than a compromise:
MediaMTX cannot replicate anyway, so a recorder pinned 1:1 to it loses no scaling that
existed. Every part of this codebase already calls it "the recorder sidecar"; on this
platform it finally is one. The `runOnRecordSegmentComplete` hook still points at
`http://recorder:8000`, resolved by a Service that selects the same pod, so one
`mediamtx.yaml` serves both deployments.

Kubernetes does **not** reverse-proxy anything itself: `Ingress` and the Gateway API are
interfaces, and a controller has to be installed to implement them. That controller is
Traefik here, and it is the same Traefik the compose stack already runs, which is why the
routing config survives the migration rather than being rewritten into a vendor's
annotations. Nothing in the design depends on Traefik specifically — see §7.

Managed Kubernetes also brings cert-manager, which is what closes the TLS item in §9 for
all three terminators at once: Traefik for the HTTP family, and Secrets mounted by
MediaMTX and Mosquitto for the protocols they terminate themselves.

#### Build the orchestrator against an interface, not a cluster **[built]**

The orchestrator targets a three-method abstraction:

```python
class FlightRuntime(Protocol):
    def start(self, flight_id: int, env: dict) -> str: ...   # returns handle
    def stop(self, handle: str) -> None: ...
    def list_managed(self) -> list: ...                      # for crash recovery
```

**Both backends are now built and tested.** `DockerFlightRuntime` runs on a laptop
against `/var/run/docker.sock`; it unblocked the whole flight lifecycle (stream live →
hook → container → stream stops → container gone) with no cloud account involved.
`KubernetesFlightRuntime` creates one Job per flight and is what makes the scaled-to-zero
GPU node pool above possible — a Job the cluster cannot place is what causes a machine to
be created, and a finished Job is what lets one be destroyed.

This was not indecision, and the split held up: the orchestrator's hard part turned out
to be exactly the lifecycle logic — reconnects, duplicate hooks, failed starts — none of
which is platform-specific, and all of which is tested against a fake runtime with no
container daemon in sight. **`flights.py` did not change by one line when the second
backend arrived**, which is the claim the design was making and is now evidence rather
than intent. `FLIGHT_RUNTIME=docker|kubernetes` selects between them.

##### What does not translate, and one thing that nearly didn't

Three settings are genuinely per-backend rather than shared, and pretending otherwise
would have been worse than admitting it:

- **`APP_GPUS` → `APP_GPU_COUNT`.** Under Docker you name cards on a host you know.
  Under Kubernetes you request a *count* and the scheduler picks the node, which is the
  entire point of the node pool. These are different questions, so they are different
  settings.
- **`APP_NETWORK` has no analogue.** Pods share a cluster network and find each other by
  service DNS. The setting is simply absent from the Kubernetes path.
- **Node selector and GPU toleration have no Docker analogue.** A GPU pool is normally
  tainted to keep ordinary workloads off it; without a matching toleration every flight
  sits `Pending` forever, which fails as a flight that never starts rather than as an
  error.

`APP_SHM_SIZE` is the one that nearly didn't translate, and it is worth recording because
the failure would have been invisible. The pipeline needs 256 MB of `/dev/shm` — the
annotation worker takes a silent SIGBUS on the runtime default a few frames in. Docker
spells that `--shm-size=256m`; Kubernetes has no such field, and the equivalent is a
memory-backed `emptyDir` mounted at `/dev/shm`, sized with a *quantity* (`256Mi`). One
`APP_SHM_SIZE` is kept, written in Docker's spelling, and translated. A pod without the
volume gets 64 MB, which the test measures as a control.

##### The service account is the point **[verified]**

The strongest standing argument for this backend was never scheduling — it was that the
Docker one holds `/var/run/docker.sock`, which is root on the host. `configs/k8s/orchestrator-rbac.yaml`
replaces it with a ServiceAccount bound to a **Role** (not a ClusterRole) permitting five
verbs on `jobs` in one namespace.

Be precise about what that buys, because it is easy to overclaim: **the privilege is not
reduced, it is scoped.** A compromised orchestrator can still start GPU workloads — that
is its job. What it can no longer do is read a Secret, create a privileged pod, touch the
hub's namespace, or reach the node.

`tests/comms/test_k8s_runtime.py` runs **under that service account's own token**, so
every call in it is simultaneously a test of the manifest: deleting `list` from the Role
makes `recover()` fail with a 403, which was checked rather than assumed. The runner adds
the other direction — eight `kubectl auth can-i` probes, three affirmative and five
refusals.

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

#### The read side: flight history **[built]**

Three more routes answer what *has* flown, which is a different question from what is
flying and is deliberately not served by the same query:

| Route | Answers |
| --- | --- |
| `GET /flights/history?limit&before&stream_id` | a page of past flights, newest first, with alert and recording counts |
| `GET /flights/{id}` | one flight: when it flew, what was archived, and a page of its alerts |
| `GET /flights/{id}/alerts/{alert_id}/image` | the JPEG crop stored with one alert |

Four decisions in there are worth stating, because each has an obvious alternative that
is worse:

- **`before` is a cursor, not an offset.** It is a `flight_id`, and a page is the flights
  below it. History is ordered newest first, so with `OFFSET` a flight taking off while
  someone reads page 1 shifts every later row down by one and page 2 repeats the row page
  1 ended on. The cursor is immune: *older than flight 91* means the same thing however
  many flights start afterwards. It orders by `flight_id` rather than `start_time` for a
  related reason — `start_time` has no unique constraint, so two flights opened in the
  same tick have no defined order between them and a page boundary landing there could
  drop one entirely.
- **The counts are two grouped queries, not two joins.** Alerts and recordings both hang
  off `flights`; joining both in one query multiplies the rows, and a flight with 3
  alerts and 2 recordings reports 6 of each. Both figures are asserted, with the
  inflating query kept beside them as a control.
- **Alert images are a route, not a field.** The live alert feed inlines the image as
  base64 because it is delivering one alert over a socket that is already open; a
  flight's history is fifty of them at once, and inlining those would be tens of
  megabytes the browser can neither cache separately nor defer. As URLs they are lazy,
  cacheable, and individually authorised. "Tens of megabytes" is not a figure of speech:
  this document calls these images crops, and they are not — `output_alert_streamer`
  stores the **full-resolution annotated frame**, unresized, so each one is a 1920×1080
  JPEG.

  That was true of the response and **false of the query**, which is the more expensive
  half and went unnoticed for as long as the claim was only ever read rather than
  measured. `flight_detail` selected the mapped entity, so every column came with it:
  the page fetched fifty full-resolution JPEGs out of the database and into db-writer
  purely to evaluate `image_data is not None` and discard them. Measured at 400 KB a
  frame, that is **19.5 MB moved per page view to produce fifty booleans**. The columns
  are now named explicitly and the database answers `image_data IS NOT NULL` itself.
  Deferring the attribute would have been worse: it fixes the one query and turns any
  later access into a lazy `SELECT` per row.
- **`public_uuid` is not in any of the three responses.** History reports what happened;
  it is not a way to reach the media path it happened on. A viewer token is still the
  only thing that opens a stream.

Every one of the three joins through `streams` and filters on `user_id` **inside the same
query that selects the row**, never fetching first and checking ownership after. That is
what makes "not yours" and "does not exist" the same 404. The image route checks both ids
— the alert must belong to the flight in the URL *and* the flight to the caller — because
`alert_id` is sequential across every tenant in the system, and these are photographs of
somebody's land.

#### The front-end

Server-rendered HTML from a small FastAPI service, no build step and no framework. The
whole surface is six pages — sign in, register, slots, watch, history, one flight — and a
toolchain would be more moving parts than the pages themselves.

| Page | What it does |
| --- | --- |
| `/login`, `/register` | The only forms that carry a password. Registration signs the new account straight in |
| `/` | Slots, each with the full `rtmps://` ingest URL to retype, plus New key / Retire, and a Watch button on whatever is live |
| `/watch` | The annotated video and the alert feed for one flight |
| `/history` | Every flight this account has flown, newest first, paged by cursor. `?stream_id=` narrows it to one slot |
| `/flights/{id}` | One flight: duration, archived recordings, and its alerts with their crops |

The history pages are ordinary links with the cursor in the query string, which is the
same statelessness the session cookie buys: an *Older* link is a URL, not a position this
process is remembering on someone's behalf. They also render an open flight as **Open**
rather than *Live*, and that is not a wording choice — a null `end_time` means nobody
closed the flight, which is usually because it is still in the air and sometimes because
the orchestrator died first (§5). The dashboard is where liveness is asserted, because
that is the page whose claim is checked against `/flights` before a Watch button appears.

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
modes, and step 7 now ends where it was always supposed to: a person signed in, pressed
Watch, and saw the annotated video play in Chrome and in Firefox. Nothing in this
lifecycle is unobserved any more — though that last step is a human's report on one
afternoon rather than an assertion, and §9 says what it does and does not cover.

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

The four public rows now describe the running stack rather than the target. Both halves
of each are built: the credential half (stream keys, per-stream MQTT users and ACLs,
per-flight JWTs, the session cookie) and the transport half (HTTPS/WSS through Traefik,
RTMPS/RTSPS on MediaMTX, MQTTS on Mosquitto). **No public credential in this system has
to cross the internet in the clear any more**, which was the largest gap between this
section and the running stack and is now closed.

"Has to" is doing real work in that sentence, and it is the honest residual: the
plaintext listeners are still published, because the same rows above keep them as a
narrow fallback for drone firmware that cannot do TLS at all. Nothing *forces* a drone
onto the encrypted port. What changed is that the encrypted port exists and the portal
prints its URL — before this, the `rtmps://…:1936` address on the slots page named a
listener that was not running, so the only ingest URL the product ever showed an
operator was one nothing would answer.

### TLS termination **[built]**

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

WebRTC media also listens on **TCP/8189 as well as UDP** (`webrtcLocalTCPAddress`), because
some networks pass no UDP at all and such a viewer would otherwise have no way to watch.
ICE picks the transport; the session, the token and the latency are unchanged. This is why
the portal offers no HLS fallback: HLS would mean a second protocol, a second player and a
second manifest-auth path to solve the same problem, at 6–30s of added latency that would
put the video visibly out of step with the alert feed beside it. Neither transport may be
proxied — both are end-to-end DTLS-SRTP.

(Traefik *does* support TCP routers with SNI, so proxying RTMPS is possible — self-termination
is chosen for the identity-preservation reason, not because of a Traefik limitation.)

**Certificates come from cert-manager, not from the load balancer**, and this is the reverse
of the usual argument. A cloud-managed certificate terminates *at* the LB and hands back no
key file — but MediaMTX and Mosquitto self-terminate and need a certificate and key on disk.
A managed certificate therefore covers only the one slice that could have gone either way,
leaving cert-manager to be run anyway for the other two: two issuance mechanisms where one
would do. cert-manager writing into Secrets that all three services mount is that one
mechanism.

**All three terminators now exist.** Traefik is in `docker-compose.yml` with its
configuration in `configs/traefik/`, terminating HTTPS and WSS for the portal, HLS,
WHEP signalling and the viewer WebSocket — the four services a browser touches.
`PORTAL_COOKIE_SECURE` and `PORTAL_PUBLIC_TLS` therefore describe a deployment that
exists, which closes the "no working configuration" corollary in §8.

The drone's three followed: `rtmpEncryption`/`rtspEncryption: optional` with cert
paths in `configs/mediamtx/mediamtx.yaml` opens RTMPS on 1936 and RTSPS on 8322, and
Mosquitto's MQTTS listener on 8883 is no longer commented out. Both read the same leaf
Traefik does. **A stream key no longer has to cross the internet in plain text** —
which mattered more than one line suggests, since that key is the ingest path as well
as the credential, is typed into a controller before every flight, and never expires.

`optional` rather than `strict`, on both MediaMTX listeners and by keeping Mosquitto's
1883 listener, is the fallback these boundaries always described. The cost of that
choice is stated plainly: a drone pointed at `rtmp://` still gets a working connection
and no warning. Switching to `strict` and deleting the 1883 listener is a two-line
change the day no drone needs it, and nothing else moves.

Two consequences of self-termination that are easy to meet by surprise:

- **MediaMTX exits at startup if the certificate file is absent.** Not a warning and
  not a disabled listener — `open /certs/server.crt: no such file or directory`, then
  `[RTSP] closing`. So `scripts/generate_local_certs.sh` is now a prerequisite of
  `docker compose up` rather than a nicety, and every test runner that mounts the real
  config had to start issuing one.
- **The TLS floor is 1.2 on all three drone-facing listeners**, matching Traefik's
  configured `minVersion` — but it comes from the Go and OpenSSL builds underneath
  rather than from a setting, because neither MediaMTX nor Mosquitto exposes one that
  works. Mosquitto's `tls_version` is deliberately left unset: in this build it caps
  the version rather than flooring it, so setting `tlsv1.2` would refuse the 1.3
  clients it should prefer and admit nothing new below. Measured rather than assumed —
  see §9.

#### Certificates before a hostname exists **[built]**

cert-manager cannot issue anything until a name resolves here, and that is an
external ask with lead time (§9). It is not, however, a reason to wait: all three
terminators read a certificate and a key from disk and none of them knows who signed
it. Public trust is only worth anything for a browser somebody else controls, and
there is not one of those yet.

`scripts/generate_local_certs.sh` therefore stands in for cert-manager — a local CA
plus one wildcard leaf covering `<domain>`, `*.<domain>`, `localhost` and the host's
own IP, since `MEDIAMTX_HOST` holds an IP today. One leaf serves all three
terminators, and it is named `server.crt`/`server.key` because that is what
`mosquitto.conf` already expected — which is now what it actually reads, along with
MediaMTX and Traefik. The single-leaf choice was made before there was a second
consumer and cost nothing when the second and third arrived.

Two details are deliberate rather than convenient. The leaf lasts **397 days**, the
browser maximum, rather than the decade a throwaway local certificate usually gets: a
certificate that never expires is a way to never discover the renewal problem. And
`--renew-leaf` reissues against the same CA without touching it, which is exactly the
file swap that question needed — the CA stays installed in whatever trust store already
has it. That is what `run_cert_renewal.sh` drives, and the question is now answered
rather than open; see below.

When a hostname lands, the ACME block at the foot of `configs/traefik/traefik.yml`
replaces the script. Nothing else in the stack changes, which is the property that
made building the tier before owning a domain worth doing.

#### Renewal: what each terminator does when the leaf changes **[verified]**

A certificate on disk is only half an answer. cert-manager will replace that file
every sixty days or so, and a service that does not reread it turns renewal into a
restart. The three do three different things, and the two that were *assumed* were
both assumed wrong:

| Terminator | Notices a replaced leaf? | What makes it |
| --- | --- | --- |
| **MediaMTX** | **Yes**, unaided, within seconds | nothing — it reads the file per handshake |
| **Mosquitto** | No | `SIGHUP` |
| **Traefik** | **No**, despite `watch: true` | `touch` any file in the watched dynamic directory |

**MediaMTX was the worry and is the good news.** It rereads by itself, and a flight
already in the air is not disturbed — the established session keeps the certificate it
negotiated while new connections get the new one. So renewal costs no restart on the
one service whose restart would drop every flight in the air. **Do not send it
`SIGHUP`** by analogy with Mosquitto: that kills the process.

**Traefik was assumed to reload and does not.** `watch: true` watches
`providers.file.directory` — the routing config — and the certificate is deliberately
mounted outside it, so replacing the leaf fires no event at all and Traefik serves the
expired one indefinitely. A `touch` on any file in that directory reloads the
configuration and the certificate with it, with no restart and no dropped connections,
so the fix is one line in a renewal hook rather than a design change. In Kubernetes it
stops being true and stops mattering: cert-manager writes a Secret and the Kubernetes
provider watches it directly.

All three are properties of somebody else's binary, which is why they are pinned by
`tests/comms/run_cert_renewal.sh` rather than written down here alone — an upgrade
could change any of them, and the symptom would arrive sixty days later.

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
| 1936 | RTMPS | MediaMTX | **published, TLS terminated by MediaMTX** |
| 8554 | RTSP (fallback only) | MediaMTX | published, in the clear |
| 8322 | RTSPS | MediaMTX | **published, TLS terminated by MediaMTX** |
| 8888 | HLS | Traefik | **HTTPS through Traefik** |
| 8889 | WebRTC / WHEP signalling | Traefik | **HTTPS through Traefik** |
| 8189/udp | WebRTC media | End-to-end DTLS-SRTP — **must not be proxied** | published direct |
| 8189/tcp | WebRTC media over ICE-TCP | End-to-end DTLS-SRTP — **must not be proxied** | published direct |
| 8765 | WSS — the viewer's alert WebSocket | Traefik | **WSS through Traefik** |
| 1883 | MQTT (fallback only) | Mosquitto | published, in the clear |
| 8883 | MQTTS | Mosquitto | **published, TLS terminated by Mosquitto** |
| 443 | HTTPS — the portal | Traefik | **HTTPS through Traefik** |

The plain-text ports are labelled *fallback only* because that is their designed role —
drones without TLS support (§7) — and they are now genuinely that rather than the only
path: every row above has an encrypted sibling that is published and working. The
browser reaches everything over TLS and so can the drone.

What remains is not a missing listener but a missing *compulsion*. Nothing rejects a
drone that dials 1935, and nothing tells its operator they are sending a permanent
credential in the clear. That is deliberate — the fallback would not be one otherwise —
and it is the reason the plain rows stay in this table rather than being deleted.

The app tier also still reaches MediaMTX and Mosquitto over the plaintext ports inside
`comms-net`, which is the in-cloud scoping decision in §7 rather than an oversight, and
is why removing those listeners is not purely a drone-side question.

Traefik publishes the four ports its upstreams used to publish themselves, and those
four no longer publish at all — the portal, HLS, WHEP and the viewer WebSocket are
reachable only through it. Leaving a direct publish in place would have been a
plaintext path around the terminator, which is the one thing a terminator cannot
tolerate. 8189 is the deliberate exception and must stay one.

`PORTAL_COOKIE_SECURE` and `PORTAL_PUBLIC_TLS` both default on and now describe the
deployment that exists. Turning them off is a local-HTTP affordance and nothing else: a
`Secure` cookie is simply not returned over `http://`, so the symptom of leaving one off
in production is not an error but a login that appears to work and then forgets the user.
That used to leave the portal with **no working configuration at all** — either the
cookie never came back or the local-only affordance ran in production. The Secure cookie
is now asserted to survive a real round trip over real TLS (§9).

`PORTAL_TRUSTED_PROXY_HOPS` **defaults to 1 rather than 0** for the same reason, and
the change is not cosmetic. Traefik is now the peer address the portal sees, so at 0 the
rate limiter counts every client on the internet into a single bucket and one attacker
locks out everybody. This is the *low* side of a variable whose documented danger has
always been the high side, and both directions are now covered: a forged
`X-Forwarded-For` is ignored, and two clients on different addresses are shown to hold
separate buckets (§9).

It must count the *real* hops between the browser and the portal, which is a deployment
fact and not a property of any product name. An L4 load balancer with source-IP
preservation adds none, so a browser → LB → Traefik → portal path is 1; a cloud L7 LB in
front of Traefik would make it 2. Counting too high lets the client name its own
rate-limit bucket (§4); counting too low collapses every client into one.

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
> each one guards and how to run it. The shell runners stand up the required containers
> and clean up after themselves. The host interpreter has none of the dependencies, so
> everything runs in throwaway containers — except `run_mediamtx_auth.sh`, which needs
> `ffmpeg` and `curl` on the host to drive real publishes and reads.
>
> Every runner that mounts the real `mediamtx.yaml` or `mosquitto.conf` now issues a
> throwaway certificate first, because both services terminate their own TLS and
> MediaMTX will not start without one.

### Where to pick up next

The rest of this section is a ledger, ordered by subject rather than by urgency. This is the
ordering — what to do next and why that one first. It is the entry point for anyone, human
or otherwise, arriving without the history.

**Two asks with lead time, worth starting before anything else because everything below
waits on them and neither is work:**

1. **A hostname and control of its DNS.** Let's Encrypt will not issue a certificate for an
   IP address, and `MEDIAMTX_HOST` is an IP today. What this gates is narrower than it
   first appeared: **only ACME issuance**, not the ingress tier. Traefik, the TLS
   listeners and the renewal question all read a certificate from disk and do not care
   who signed it, so all three were built and the renewal behaviour of all three was
   measured against a local CA (`scripts/generate_local_certs.sh`); the handover is one
   config block. That is now the whole of what waits on a name. What a real
   name buys is a browser somebody else controls — which is why it is still worth
   starting now and not urgent to finish. Prefer a registrar whose DNS has an API
   cert-manager supports: the tier wants a wildcard, and a wildcard needs DNS-01.
2. **A DEM raster covering the operating area** (`dem/dem.tif`, `dem/dem_mask.tif`). Slope
   and no-data analysis are skipped without it, so a real part of `danger_detection` has
   never executed.

**The ingress tier is finished, the product has been watched working in a browser, both
`FlightRuntime` backends exist, the portal shows what has flown as well as what is
flying, and the hub tier now has manifests that deploy on a real cluster.** Between them
those closed every item that has stood at the top of this list.

**There was no code item left here**, and that stopped being true when §10 was
written. What this ledger describes is finished; §10 is a decided direction that
changes the credential model, the flight lifecycle and the media tier's shape, and
none of it is built. Read this section as the state of what exists and §10 as where
it goes next.

Setting §10 aside, what remains is the two asks above, plus the
provider-specific values that only exist once a cluster does — a registry to push the
five images to, a storage class if the default is not wanted, and the load-balancer
addresses that `configs/k8s/endpoints.env` needs. None of those is work that can be
done here.

One claim this list used to make is worth retracting, because it was wrong in a way
that cost time: it said the hub manifests **"can only be tested against a cluster
nobody is running"**, and that this made them not worth starting. `run_k8s_runtime.sh`
had already disproved it next door — k3s in a container is a real API server, a real
scheduler and a real kubelet, and it starts in seconds. The manifests were written and
verified without a cloud account, and the two defects that turned up in doing so
(§2, and the entry in *Built and tested*) would both have shipped straight into the
first paid cluster.

Five things left this list rather than being completed by it, and the distinction
matters because each leaves a residue:

- **Certificate renewal** is answered (§7), and leaves one line for whoever writes the
  deployment hook: **it must `touch` a file in Traefik's watched directory**, because
  Traefik is the terminator that does not notice a replaced leaf on its own. That
  applies to the compose deployment. Under the manifests it does not: cert-manager
  rewrites the `agrarian-tls` Secret, the kubelet refreshes the projected files in
  place, and Traefik's file watcher sees it. **Mosquitto's `SIGHUP` still has no
  answer on either platform** — nothing in the manifests sends one, so that remains a
  job for whatever drives renewal.
- **The watch page** has been loaded in Chrome and Firefox and the video plays, which
  was the whole question. The smaller half it left behind — the alert aside and the
  page on a phone — has since been looked at too, along with the two history pages,
  and is closed rather than pending; the three defects that pass found are in *Built
  and tested*.
- **The ingress tier** is built on both sides. It leaves the choice of when to make TLS
  compulsory, which is below and is not work.
- **The Kubernetes `FlightRuntime` backend** is built, and with it the service account
  that retires the Docker socket (§2). The rest of the migration it used to leave — the
  hub services having no manifests — is now built too, and both are above.
  It also leaves a knob this platform offers and Docker does not: `activeDeadlineSeconds`
  would cap a single flight's GPU hours. It is deliberately unset, because a backend that
  ends flights the other one would not is a backend the abstraction no longer hides. It
  belongs with quota (*Open*), not here.
- **Flight history** is built (§4, *Built and tested*), and it turned out to be the read
  side of everything the system already recorded rather than a feature of its own: no
  table changed, no column was added, and the orchestrator was not touched. What it
  leaves is smaller than the item was and is in *Open*: a recording is shown as a
  storage location rather than offered as a download, because handing one over means the
  portal holding object-storage credentials or db-writer signing URLs, and neither is a
  history feature.

Not on that list, and worth saying why: **making TLS compulsory on the drone side.**
Every encrypted listener now exists, and the plaintext ones remain by design (§7, §8).
Turning `optional` into `strict` is a two-line change, and the thing gating it is not
work — it is knowing whether any drone that will actually fly here needs the fallback.
That is a question for whoever owns the aircraft, not a task.

Everything else in *Open* is either genuinely conditional (MediaMTX sharding, TURN over 443,
auth-endpoint caching — all of which want a measurement or a user complaint first) or paired
with a feature that does not exist yet (email verification with password reset, quota with
billing).

### Built and tested

- **The hub tier as Kubernetes manifests, deployed on a real cluster** (§2, 2026-08-03).
  `configs/k8s/hub/` plus a kustomization at `configs/`, applied with
  `kubectl apply -k configs/`. All eight hub services come up: Redis, db-writer,
  ws-server, portal, orchestrator, MediaMTX (with the recorder beside it), Mosquitto
  and Traefik.

  **The ConfigMaps are generated from the config files the compose stack already
  mounts**, not copied from them. That is why the kustomization sits at `configs/`
  rather than in `k8s/` — kustomize refuses to read above its own root, and the root
  has to contain `mediamtx/`, `mosquitto/` and `traefik/`. Obeying that constraint
  beats working around it with `--load-restrictor`: two copies of `mediamtx.yaml` is
  the same defect §4 rejects in `authInternalUsers` and in Mosquitto's
  dynamic-security plugin — a second store kept in sync by hand, wrong silently.

  Verified by **37 assertions** in `tests/comms/run_hub_manifests.sh`, against k3s in
  a container with the five service images built and imported. Seventeen read the
  render before anything runs; twenty ask the live cluster.

  **Two defects were found that reading the manifests could not have caught**, and
  both were silent:

  - **kustomize's `namespace:` transformer rewrites `Namespace` objects themselves.**
    It collapsed `agrarian-flights` into `agrarian`, which would have given the
    orchestrator's Role authority over Jobs in the namespace holding db-writer, Redis
    and Mosquitto — undoing the entire security argument of
    `orchestrator-rbac.yaml`. kustomize reports this as an "ID conflict", which does
    not sound like what it is. There is now no global namespace transformer; every
    manifest names its own.
  - **A `configMapGenerator` without an explicit namespace generates into `default`
    *and* silently declines to stamp its content hash into the references.** The build
    succeeds and every mount dangles. The failure surfaces only as pods stuck in
    `ContainerCreating`.

  The second one also broke the assertion written to catch it, which is worth
  recording because it is the failure mode this whole test directory is built against.
  The first version compared reference names against a regex for the unhashed
  spelling; the bare name sits at the end of a line, the pattern required a trailing
  character, and so it **matched nothing whether or not the bug was present** — it
  passed the control. It now resolves every `configMapRef`, `configMapKeyRef` and
  `configMap` volume against the ConfigMaps actually in the render, in the same
  namespace, and reports one dangling reference against the reintroduced bug and zero
  without it.

  Both defects were confirmed by reintroducing them. The namespace one fails loudly at
  build time; the generator one is the quiet one and is why the referential check
  exists.

  Also asserted, each pinning a claim made elsewhere in this document: the portal
  manifest carries neither the session secret nor the database secret while
  db-writer's carries the first (§7, as a manifest property and again as the running
  pod's environment); db-writer, the alert-write API, Redis and the orchestrator are
  all `ClusterIP` (§8); Traefik's Service does not carry 8189 (§8); the orchestrator
  mounts no Docker socket, runs `FLIGHT_RUNTIME=kubernetes`, and under its own
  ServiceAccount may create Jobs in the flight namespace but not in the hub namespace
  and cannot read a Secret (§2); the recorder is a container in the MediaMTX pod with
  both containers mounting the claim, and no separate recorder Deployment exists; and
  MediaMTX opens **RTMPS** on 1936, which is the strongest evidence available that the
  TLS Secret mounted and parsed — it exits at startup when the file is missing, so the
  plaintext listener alone would prove nothing.

  **Not covered: the GPU, the load balancers and the cloud.** k3s's ServiceLB assigns
  node addresses rather than provisioning anything, so a `LoadBalancer` here proves the
  Service spec is accepted and routes — not that a provider will carry mixed TCP and
  UDP on one address. That constraint is real and the first paid cluster is where it
  stops being spec. The images are placeholders (`ghcr.io/REPLACE_ME/…`) that the
  runner substitutes, and the flight app is never started, because there is no GPU and
  no device plugin — the same gap `run_k8s_runtime.sh` already records.
- **Every page looked at in a browser, at desktop and phone width** (§4, 2026-08-03).
  The last of the "asserted but unobserved" UI items, and the one that closes the
  *Open* entry that used to sit here naming two corners of the watch page and the two
  history pages. Driven against `run_watch_live.sh` — a real flight on a real GPU —
  with Chrome under playwright at 1440px and at 390px, alerts injected on the Redis
  channel for the live aside and 57 written through db-writer's own alert route for
  the flight page.

  **Two of the three corners were fine and one was not.** The alert aside renders,
  newest first, with the crop decoding and `<script>alert(1)</script>` arriving as
  text — the `textContent` path holds. The watch page stacks correctly on a phone,
  because it is the one page anyone made responsive: its breakpoint was the *only*
  `@media` rule in the stylesheet. Playback re-confirmed in passing — `readyState 4`,
  1920×1080, `currentTime` advancing, exactly one WHEP POST answering 201, no request
  to the reader page.

  Three defects were found and fixed, and the first is the one worth remembering:

  - **`/history` widened the layout viewport and zoomed the whole page out.** A
    six-column table with a `nowrap` timestamp cannot fit 390px, and a browser does
    not clamp it — it grows the layout viewport instead. Measured: `innerWidth` came
    back **663** on a 390px device, every word on the page at 59%. **The obvious check
    cannot see this**, because `scrollWidth > innerWidth` never fires when
    `innerWidth` is the thing that grew; the first pass of this work asserted no
    overflow and was wrong. Fixed with a `.table-wrap` scroll container — which was
    inert until `.card` also got `min-width: 0`, since a grid item refuses to shrink
    below its content by default.
  - **The ingest URL broke mid-key.** `word-break: break-all` rendered it as an
    eleven-line ribbon four characters wide. This is the one string a human
    transcribes by hand before every flight (§3) and a stream key has no safe break
    point, so it is now `nowrap` and scrolls inside its own box.
  - **The alert grid stretched every card to the tallest in its row**, turning the
    ~1-in-5 alerts with no image into large empty bordered boxes. `align-items: start`.

  Fixing the first one moved **Watch** — the primary action of the product — off
  screen behind a horizontal scroll with nothing to say so, so the slot table stops
  being a table below 640px (`.slots-stack`). The history table deliberately does not:
  its columns are bare numbers that mean nothing without their headers, so it stays a
  table and its first column became a link instead.

  **One thing this turned up that is not a UI matter at all: an alert image is not a
  crop.** `output_alert_streamer._process_alert` stores the *full-resolution annotated
  frame* — `height, width = frame.shape[:2]`, no resize before `cv2.imencode` — so
  every alert row holds a 1920×1080 JPEG at quality 85. This document calls them crops
  throughout. It strengthens §4's reasoning for making alert images a route rather than
  a field, and it means a flight page loads fifty full-HD frames: ~78 KB each for the
  test pattern measured here, and several times that for real aerial imagery, which
  compresses far worse than colour bars.

  `run_portal.sh` (118/118) and `run_portal_auth.sh` (51/51) both still pass against
  the changed templates. What is still not covered is that **nothing re-checks any of
  this** — it is the same standing gap as the video observation below, and a change to
  the stylesheet can undo it silently.
- **Flight history in the portal** (§4, 2026-08-03). Three read routes on db-writer, two
  pages on the portal, and no schema change of any kind — every row this reads was
  already being written. Covered twice, at the two levels where it can be wrong:

  `tests/comms/test_flight_history.py` — **50 assertions**, SQLite in memory, no stack —
  is the query layer. `tests/comms/run_portal.sh` — now **118 assertions**, up from 88 —
  drives the pages through real HTTP against real PostgreSQL: alerts written by the app's
  own route, a segment logged by the recorder's, the flight closed by the orchestrator's,
  and then the history read the way a browser reads it.

  Two properties carry **controls that fail**, because both are the kind of claim that
  passes vacuously against small data:

  - **Cursor paging does not repeat a row** when a flight takes off mid-browse. The
    control runs `OFFSET` over the same rows at the same moment and *does* repeat one —
    so the property is a fact about this data, not a belief about paging.
  - **Alert and recording counts do not inflate each other.** The control is the single
    joined query, which reports a flight with 3 alerts and 2 recordings as having **6 and
    6**.

  Tenancy was checked by breaking it: deleting the `user_id` filter from the history
  query, and the flight check from the image lookup, fails 10 of the 45 — including
  "another tenant's flight is absent" and "a real alert id under the wrong flight is
  refused". A test that cannot fail this way is not testing isolation.

  What was **not** covered at the time: nobody had looked at either page in a browser.
  The markup is asserted, the crop is byte-compared through two services, and neither of
  those is the same claim as "the grid of fifty images looks right". Both pages have
  since been looked at, at desktop and phone width — see the entry above, which is also
  where the three defects that found are recorded.

- **The Kubernetes `FlightRuntime` backend, against a real API server** (§2, 2026-08-03).
  `KubernetesFlightRuntime` creates one Job per flight; `FLIGHT_RUNTIME` selects it.
  `tests/comms/run_k8s_runtime.sh` — **65 assertions** — stands up a k3s cluster in a
  container and drives the real thing: a real API server, a real scheduler, a real
  kubelet. No mock has an opinion about whether a Job spec is schedulable.

  The result that matters most is the one that cost nothing to state and would have cost
  a lot to miss: **`flights.py` did not change**. Reconnects, duplicate hooks, failed
  starts and crash recovery all run unmodified on the second backend, which is what §2
  claimed in advance and can now stop claiming.

  Three properties were **falsified rather than asserted**, because each is the kind that
  passes vacuously:

  - `/dev/shm` is 256 MB in a running flight pod. The control is the same image in a Job
    without the `emptyDir`: **64 MB**, the value that SIGBUSes the annotation worker.
    Without the control this measures Alpine, not the volume.
  - `stop()` removes the pod, not just the Job. Rebuilt with `propagation_policy="Orphan"`
    and confirmed the check fails — the Job disappears and a pod keeps running with
    nothing owning it, which on a GPU node pool is the most expensive mistake in the file.
  - The RBAC manifest is load-bearing. The test holds the orchestrator's **own service
    account token**, so removing `list` from the Role was confirmed to fail `recover()`
    with a 403 rather than passing quietly under admin credentials.

  Not covered, and it is the obvious gap: **there was no GPU and no device plugin.** The
  `nvidia.com/gpu` limit, the node selector and the toleration are checked as spec — the
  Job body the API server would receive — because a cluster with no GPU cannot schedule a
  pod that asks for one. The first real cluster is where those three stop being spec.
- **A person watched the annotated video play, in Chrome and in Firefox** (§6 step 7,
  2026-08-03). The oldest open item on this branch, and the only one no automated test
  could ever reach. `run_watch_live.sh` put a real flight in the air on a real GPU;
  signing in at the portal and pressing **Watch** produced moving video in both engines.

  This is a **human observation, not an assertion**, and it is listed here rather than
  quietly folded into the playback entry below because the distinction is the whole
  point of the item: everything underneath was already verified — WHEP returns 201, a
  real client decodes 1920×1080, the URL shape is pinned by 88 assertions — and none of
  that answered whether a browser would show a picture. It now has, once, on one
  afternoon. Nothing re-checks it, so a change to `watch.js` or the `<video>` element
  can break it silently; that is the price of the only claim here a machine cannot make.

  What it settles is the specific risk §9 had been carrying: **autoplay policy**. The
  element is `autoplay muted playsinline`, muted being what makes autoplay legal without
  a click, and both Blink and Gecko accepted it. What it does not settle is the rest of
  the page — see *Open*, which is now two corners rather than the whole thing.
- **Certificate renewal, on all three terminators** (§7). The open item with a deadline
  attached — the answer was needed before the first real certificate was issued, not
  before it expired — settled by 15 assertions in `run_cert_renewal.sh`, which issues a
  leaf, stands the three services up against it, reissues with `--renew-leaf` under a
  **live authorised flight**, and asks each of them what it is serving on a fresh
  connection.

  The result was the opposite of what this document assumed, in both directions.
  **MediaMTX — the service whose restart would drop every flight in the air — rereads
  the file by itself within seconds, and the flight in the air is undisturbed.**
  **Traefik, which §7 asserted picks up a changed file, does not**: `watch: true`
  watches the routing directory and the certificate is mounted outside it, so nothing
  fires. Mosquitto behaves as documented, on `SIGHUP`.

  Three of the assertions are the ones holding the rest up. The renewal is checked to
  have issued a *different* serial, without which every later assertion passes while
  measuring nothing. The publish is checked to have been *authorised* before the
  renewal, because an earlier version of this ran without db-writer and "the publisher
  survived" was a statement about ffmpeg retrying a refused connection. And **SIGHUP is
  asserted to kill MediaMTX**, recorded as a test rather than a warning so that nobody
  reaches for the obvious symmetry with Mosquitto when writing the renewal hook.
- **RTMPS, RTSPS and MQTTS — the drone-facing half of the ingress tier** (§7, §8).
  MediaMTX and Mosquitto terminate their own TLS, reading the same leaf Traefik does,
  so a stream key no longer has to cross the internet in plain text. That key is the
  ingest path as well as the credential, is typed into a controller before every
  flight, and never expires, which is why it was the last thing worth encrypting.

  Verified by **42 assertions** in `run_ingress_tls.sh`, against the repo's own
  `mediamtx.yaml` and `mosquitto.conf` with a real db-writer and PostgreSQL behind
  them, two tenants throughout. The assertions split into two independent claims, and
  the second is the one a transport change is most likely to break quietly:

  - **The transport.** Each of the three listeners serves the leaf this run issued,
    refuses a client that does not hold the CA, and floors at TLS 1.2 — 1.0 and 1.1
    refused, 1.2 and 1.3 accepted. ffmpeg publishing with a *different* run's CA is
    refused, and the identical publish with the right CA succeeds, so the refusal is a
    statement about the certificate rather than about anything else in that container.
  - **Authorisation is unchanged by it.** A revoked key cannot publish over RTMPS or
    RTSPS, an unknown one cannot either, a publish to another flight's output path is
    still refused, and over MQTTS the drone still cannot publish under another tenant's
    key nor the app subscribe to another tenant's telemetry. Encrypted and authorised
    are independent properties, and it is entirely possible to gain one while silently
    losing the other.
  - **The plaintext fallback still works**, on both planes, which §7 requires — a change
    that broke it would ground exactly the drones the fallback exists for.

  Two things the work turned up, both about tests rather than about TLS.

  **The TLS-floor assertion in `run_traefik_tls.sh` was vacuous, and is fixed.** It
  drove `curl --tlsv1.1` and asserted the failure — but modern curl refuses to *send*
  a TLS 1.1 ClientHello, so that assertion fails identically against a server that
  happily accepts TLS 1.1. It measured the client. Both runners now use an OpenSSL old
  enough to still offer it, with a control that proves so against a server pinned to
  1.1 in the same container; without that control it would be the same vacuous
  assertion with a different binary. The reading is handshake-completed rather than
  reported version, because OpenSSL's `New, TLSv1.x` line names the era of the
  negotiated *cipher* and not the protocol — a TLS 1.1 connection can print `TLSv1.0`.

  **MediaMTX exits at startup when its certificate file is missing**, rather than
  starting without the encrypted listener. Six other runners mount the real
  `mediamtx.yaml` and none of them supplied a certificate, so this change broke all of
  them at once — caught because `run_traefik_tls.sh` went from 30 passing to two 502s.
  They now issue a throwaway leaf each, into a temporary directory rather than into
  `certificates/`, whose CA may already be installed in a browser.
- **Traefik, terminating TLS for everything a browser touches** (§7, §8). The portal,
  HLS, WHEP signalling and the viewer WebSocket are now reachable only over HTTPS/WSS
  through `configs/traefik/`, and the four upstreams no longer publish a port of their
  own — a direct publish would have been a plaintext path around the terminator. 8189
  stays published and unproxied, because WebRTC media is end-to-end DTLS-SRTP and
  proxying it would terminate the encryption the media path is built on.

  Certificates come from `scripts/generate_local_certs.sh` rather than from ACME, and
  that is what made the work possible before the hostname arrives: all three terminators
  read a key from disk and none asks who signed it.

  Verified by 31 assertions in `run_traefik_tls.sh` — the repo's own Traefik
  configuration in front of a real portal, ws-server, MediaMTX, db-writer and
  PostgreSQL. Three are worth naming:

  - **The Secure session cookie survives a real round trip.** This is the §8 corollary
    finally discharged. Every other runner drives the portal over plain HTTP with
    `COOKIE_SECURE` left on, which asserts the cookie's *attributes* and never that a
    browser would send it back — and a `Secure` cookie is not returned over `http://`.
    Here it is set over TLS and accepted on the next request.
  - **Two clients hold two rate-limit buckets.** `PORTAL_TRUSTED_PROXY_HOPS` had to
    change from 0 to 1 the moment a proxy landed, because the peer address the portal
    sees is now always Traefik's. Confirmed non-vacuous by `PORTAL_HOPS=0`, which fails
    exactly this assertion and nothing else. The existing danger — a hop count set too
    *high* — is covered separately by a forged `X-Forwarded-For` that mints no fresh
    bucket.
  - **A client without the CA is refused.** Asserted alongside the successful one, since
    a TLS test that passes against any certificate is asserting nothing. The TLS floor is
    pinned the same way — 1.2 and 1.3 accepted, 1.1 refused — though that half of it was
    asserting nothing until the drone-side work above found and fixed it.

  Two assertions failed on the first run and both were the test's fault, in ways the
  document had already warned about. HLS answered 302 because MediaMTX redirects to
  `?cookieCheck=1` *before* it authenticates (§4) and the client refused redirects —
  the exact trap that section describes. WHEP answered 400 because MediaMTX checks the
  content type before the credential, so a POST without `application/sdp` never reaches
  the hook. Driven properly both return 401 from `/auth/mediamtx`, which is what proves
  the request crossed Traefik rather than dying in it.

  What this does not cover is whether video plays through the proxy: no stream is
  published, so HLS and WHEP are driven only as far as the authorization decision.
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
- **Playback works, and the way it was built did not.** The watch page used to embed
  MediaMTX's own reader page in an `<iframe>`, on the reasoning that reimplementing WHEP
  would put the portal in the middle of a media path that should be
  browser-to-MediaMTX. The reasoning was right and the mechanism was impossible:
  `GET /<path>/` answers **401 with `WWW-Authenticate: Basic` and never calls
  `/auth/mediamtx` at all**. Not for `?jwt=`, `?token=`, `?user=&pass=`, `Authorization:
  Bearer`, or HTTP Basic — five forms tried, five 401s, and db-writer logged no decision
  for any of them. That page is gated behind MediaMTX's internal user roster, which is
  exactly what `authMethod: http` replaced (§4), so no credential this system can mint
  would ever have opened it. The earlier guess — that MediaMTX was dropping the query
  string on its way to WHEP — was wrong in an instructive way: it never got that far.

  Everything underneath the page was already correct. HLS with `?jwt=` serves real
  annotated media (`ffprobe`: H.264 1920×1080), and the WHEP endpoint authorises the same
  token and reaches the hook as `protocol='webrtc'`. So the fix was to drop the iframe and
  negotiate WHEP in `watch.js` against a `<video>` element — non-trickle, all candidates
  gathered before the offer, one POST and no PATCH. Verified with a real WebRTC client
  (`aiortc`): **201 on the offer, then decoded 1920×1080 video frames.** The media path is
  still end-to-end DTLS-SRTP browser-to-MediaMTX; only the signalling moved into our code.

  `run_portal.sh` pins the new shape at 88 assertions, including a regression check that
  the URL is the WHEP endpoint and *not* the reader page — the one thing that must never
  quietly come back.
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

### Known weaknesses in what exists

Distinct from the section below: these are live weaknesses on this branch right now, not
work that has yet to start.

- **The DEM is absent, so part of the geo stage is untested.** `dem/dem.tif` and
  `dem/dem_mask.tif` are gitignored and not on any machine here, and `open_dem_tifs()`
  returns `None` for a missing raster, so `danger_detection` runs with slope and no-data
  analysis skipped. Geofencing and the safety radius do run and are exercised. This is a
  supported degraded mode rather than a fault — the harness reports which of the two it
  got — but no test has yet driven a real elevation raster through `extract_dem_window`
  and the window cache.
- **Every alert stores a full 1920×1080 frame as `bytea`.** `image_data` is a
  `LargeBinary` column, and `output_alert_streamer._process_alert` writes the whole
  annotated frame to it unresized. The alert cooldown is **1.0 s**, so a persisting
  danger condition writes up to 3600 full-HD JPEGs an hour into the database, per
  flight. At 400 KB a frame that is over a gigabyte for a heavy hour.

  Two things make this a weakness rather than an emergency. PostgreSQL stores a `bytea`
  over 2 KB out of line in TOAST, so a query that does not name the column does not read
  it — which is why the `flight_detail` fix above mattered so much and why the remaining
  cost is disk, backup size and WAL volume rather than query latency. And nothing renders
  these anywhere near their stored size: the history grid cell is `minmax(260px, 1fr)`
  and the live aside is a column, so the stored asset is roughly twenty times the linear
  dimension of any consumer.

  **The cheap fix is to downscale before encoding, not to move the bytes.** Object
  storage is the eventual answer and is consistent with recordings, but it is the larger
  change: it needs a new holder of storage credentials (db-writer holds none today; the
  recorder does), and it must not be done by handing out pre-signed URLs. §4's alert
  image route checks that the alert belongs to the flight *and* the flight to the caller,
  in the query that selects the row, because `alert_id` is sequential across every tenant
  and these are photographs of somebody's land — and a pre-signed URL is a bearer
  credential that bypasses exactly that check for its lifetime. A storage key with
  db-writer streaming the bytes keeps the property; a URL in a row is also the thing §5
  refuses for `output_path`, for the same reason.
- **The orchestrator holds the Docker socket — under the backend this repo runs.**
  Anything that can reach its port can start containers on the host. Its port is
  internal-only, and this was the strongest argument for the Kubernetes backend.

  That backend now exists and the alternative is built: `FLIGHT_RUNTIME=kubernetes` plus
  `configs/k8s/orchestrator-rbac.yaml` gives a service account that may create Jobs in one
  namespace and nothing else (§2). **The weakness stays on this list anyway**, because
  what is deployed here is still docker-compose and still mounts the socket. Having the
  fix available is not the same as running it, and this section is about what is true on
  this branch right now.

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

- **The cloud L4 load balancer and cert-manager (§7).** Deployment-time pieces with
  nothing to build locally: the LB is a managed resource and cert-manager replaces
  `scripts/generate_local_certs.sh` when a hostname resolves here. The manifests
  already expect both — three `LoadBalancer` Services, and an `agrarian-tls` Secret
  that `configs/k8s/secrets.README.md` shows as a cert-manager `Certificate` with a
  hand-made fallback.

The hub manifests have left this list — see *Built and tested*. What they leave behind
is not manifest work: a registry to push five images to, and the load-balancer
addresses that only exist once a provider has assigned them.

The ingress tier itself has left this list. Both halves are built and tested — see
above — which is why §8's port table no longer has a "not configured" cell in it.

The Kubernetes `FlightRuntime` backend has left it too, and it took the *only* piece of
the cluster migration that was ever a design question with it. What remains above is
translation work.

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
- **MediaMTX is the one hub component a load balancer cannot scale.** A path lives on
  exactly one instance — the one the flight's app container published to — so a viewer of
  `out/<uuid>` must reach *that* instance. Round-robin an L4 load balancer across MediaMTX
  replicas and viewers land on instances that have never heard of the path. This is the
  weak point in §2's "the hub scales on load": ws-server was deliberately made stateless
  through Redis pub/sub and db-writer holds nothing, but MediaMTX is stateful per path and
  no amount of load balancing changes that. The options when it matters are path-aware
  routing (the portal already knows which instance holds each flight, since flights are
  assigned when they open), a relay tree where replicas pull from the origin with
  `source: whep://…`, or sharding flights across instances by path.

  Not urgent, and worth being clear why: one MediaMTX serves far more concurrent viewers
  than this system can produce flights, because each flight costs a whole GPU container
  and each viewer costs a peer connection. **The GPU tier saturates first, by orders of
  magnitude.** This becomes real only once the node pool in §2 is large.
- **ICE-TCP does not cover a network that permits only 443.** `webrtcLocalTCPAddress`
  handles the common case — firewalls that pass TCP but no UDP — in the same WHEP session,
  with the same token and the same latency, which is why the portal dropped its HLS
  fallback rather than vendoring a second player. What it does not handle is a network
  where nothing but 443 leaves at all: port 8189 is as blocked as 8189/udp was. The real
  answers there are TURN over 443 or HLS proxied through the ingress tier on 443 (§8
  already routes HLS through it). The ingress tier they were waiting on now exists, so
  what is left gating them is a reason: neither is worth building before a real user
  reports being unable to watch.
- **A recording is a location, not a download.** History reaches every row the system
  records, but the recordings table stores a blob name or a path on the recordings
  volume, and the portal shows it as text. Handing the segment over means either the
  portal holding the deployment's object-storage credentials — which §7 keeps out of the
  tier facing the internet, for the same reason it keeps `SESSION_JWT_SECRET` out — or
  db-writer minting pre-signed URLs, which is a small feature and a real decision about
  which service owns storage credentials. Neither is a history feature, which is why
  this is here and not in what was built.
- **A flight's alert list stops at fifty.** The detail page shows the most recent page
  and says how many there are, so a truncated list never passes for a whole one, but
  there is no *older alerts* control the way there is for flights. The cursor paging is
  already written one layer down; this is the same pattern applied a second time, and it
  waits for someone to actually hit the ceiling.
- Recorder per-tenant upload prefixes
- **TLS certificate issue.** What remains of what used to be "issue and renewal": the
  issuing half waits on a hostname, since cert-manager cannot ask Let's Encrypt for an
  IP. Renewal is no longer open — see the reload table in §7 — and the only thing it
  leaves for the deployment is that a renewal hook must `touch` a file in Traefik's
  watched dynamic directory, because Traefik alone does not notice a replaced leaf.
- **Auth-endpoint caching and db-writer replica count.** Every publish and every read
  now costs one indexed lookup here. A short-TTL cache is the obvious fix and the wrong
  one to reach for blindly: it delays revocation of a credential that has no expiry.
  Replicas first, cache only if measurement demands it.

---

## 10. Ephemeral keys and elastic media capacity **[designed]**

This is a decided direction, recorded here because it changes three things §3, §5 and
§6 currently state as settled: the stream key stops being persistent, the GPU container
stops being spawned by the media server, and MediaMTX stops being a single instance.
Sections above describe what runs today and remain accurate as such.

**Almost none of it is built.** Items land one at a time and are marked here as they
do, so that this section never reads as more finished than it is:

| Landed | |
| --- | --- |
| `RECONNECT_GRACE_S` 30 s → 120 s (§10.2) | **[built]** 2026-08-06 |
| `recordSegmentDuration` 1 h → 24 h (§10.2) | **[built]** 2026-08-06 |

Everything else below is **[designed]**.

The architecture rests on two pillars that were never examined together: a drone
**arrives unannounced**, and its key is **stable until revoked**. Each forces real
structure. Unannounced arrival is why the whole ingest path must stay warm — MediaMTX
listening, db-writer answering the auth hook, the orchestrator waiting for
`runOnAvailable`. Key stability is why the media tier cannot be resized: a key printed
into a controller months ago names a host that must still answer.

They are not equally load-bearing. **Dropping the second dissolves most of the first**,
and that is the whole of this section.

### 10.1 The key becomes the announcement

A stream key is minted **per flight**, at the moment the operator asks for one, and
dies when the flight does (§10.2). It is not a slot that persists across months.

The step this appears to add is a step that already exists. §6 records that the key is
deliberately *not* shown once and hidden — `list_streams` returns it every time,
**because the operator has to read the ingest URL before every flight**. So an operator
is already at the portal, immediately before takeoff, asking where to publish. That
page load *is* an announcement; it was simply never treated as one.

Three things follow, and the third is the one that matters most:

- **The credential stops being permanent.** §3 spends real effort on a key that never
  expires: instant revocation as the only mitigation, and the residual that the key
  appears in MediaMTX access logs. A key that lives one flight makes most of that
  argument unnecessary rather than better-defended.
- **Shard assignment becomes trivial.** A key created seconds before takeoff is
  assigned to whichever media cell has capacity *now*. There is no stored binding to
  keep valid, nothing to rebalance, and no printed URL that can go stale — which is the
  entire difficulty of sharding a media server that a permanent key creates.
- **Capacity gets a closed loop.** A key request is a capacity request. This is what
  the *Open* entry on MediaMTX sharding lacked: under permanent keys, adding an
  instance relieves nothing, because every key already in circulation is bound
  elsewhere. The response could not move the signal. Now it can.

**This does not license scaling the hub to zero**, and the reason is worth stating so
the mistake is not made later. The portal must be up to mint the key, db-writer to
answer the auth hook the instant the drone connects, Redis and PostgreSQL underneath
both. What ephemeral keys remove is the *media* tier's obligation to be warm — and the
remaining warm set still needs a machine, on which MediaMTX's request fits in the
slack. The saving is single-digit dollars a month against a GPU tier measured in
hundreds. **Scale-to-zero is worth doing where the GPU is and nowhere else.**

### 10.2 Two timers, not a duration

The key carries no user-chosen lifetime. It lives exactly as long as the work does,
bounded by two timers with different jobs:

| Timer | Fires when | Action |
| --- | --- | --- |
| **Pre-flight**, 15 min | key minted, nothing ever published | tear down, free capacity, revoke |
| **Post-flight**, N min (≈10) | last disconnect, no reconnection | tear down, free capacity, revoke |

A user-selectable duration was considered and rejected: no flight approaches the
shortest value anyone would offer, so it is a knob whose every setting means the same
thing. The timers already know when the work is over.

**The pre-flight timer must be stated on screen** — *"if no stream starts within 15
minutes this session is discarded"* — with what to do about it. A silent timeout is a
defect; a stated one is a contract, and recovery is one click. Fifteen rather than ten
because the interval that actually elapses is not "copy a URL": it is walk to the
launch point, props on, GPS lock, preflight. A careful operator can spend ten minutes
without being at fault, and the cost of generosity is cents of idle GPU against a
re-do in a field.

**`RECONNECT_GRACE_S` does a different job and has moved to 120 s. [built]** It decides
*flight identity* — inside it the same flight, container and `flight_id` continue;
outside it the next takeoff is a new flight, which §6 already calls the honest
description. The post-flight timer decides *key and capacity lifetime*, and is
deliberately much longer.

The value moves from 30 s because the asymmetry that set it has reversed. It used to be
one-sided: erring long cost idle GPU and nothing else, so generosity was free. Now that
a battery swap must be recorded as a separate flight, erring long has a *semantic* cost
too — a practiced operator with the drone at their feet can be airborne again in under
three minutes, and a grace window that long would silently merge two flights into one.
120 s sits in the gap: a dropout behind a treeline is 30–60 s and is comfortably
covered, while the fastest realistic swap is not. 180 s starts eating into it.

That gap between them is the design's best property and it is easy to miss. A battery
swap takes two to five minutes, so it falls **outside** the grace window and **inside**
the post-flight timer. The result: the swap is honestly recorded as a second flight,
and **the container stays warm across it** — model weights already resident, so the
second sortie starts immediately instead of paying a cold GPU start. A session-scoped
key would have kept the credential alive and still torn the container down at grace
expiry, which is the expensive half.

The cost is idle GPU between sorties — roughly five minutes at swap, and up to N
minutes for an operator who simply goes home. That is the trade N sets, and it is
cheap in both directions. An explicit **End session** control in the portal makes the
common case immediate and leaves the timer as the safety net for people who forget.

**The recording needs nothing from teardown.** MediaMTX always closes and flushes the
current segment on publisher disconnect regardless of `recordSegmentDuration` (§9), so
the upload has already run by the time either timer fires. Teardown frees capacity and
nothing else.

That same flush is what gives the archive its shape, and it makes one invariant free:
**no recording can ever span two flights.** Every flight boundary is a disconnect — that
is what ends a flight — and every disconnect closes the segment. The boundary is clean
at any grace value.

`recordSegmentDuration` therefore rises to **24 h [built]**, from the 1 h that was only
ever a placeholder: no consumer airframe flies long enough for an hourly split to fire,
so the setting has never once divided a real flight. With the ceiling raised, one flight
produces exactly one recording — except when the publisher drops and returns inside the
grace window, which produces one per connection interval. `flights → recordings` is
already 1:many, so that case needs nothing.

It is **raised rather than deleted**, and the reason is a mistake this document has
recorded once already: §4 describes SRT and MoQ running on every start in v1.19 because
they were enabled *by default* and nobody had decided so. An omitted line inherits
whatever the next version's default happens to be. The value is also quoted in the
`recordings` PersistentVolumeClaim comment in `configs/k8s/hub/mediamtx.yaml`, which
moves with it.

#### Revocation is what makes the key ephemeral

Teardown revokes the key. Without that step nothing else in this section is true: a key
that outlives its flight is a permanent key, and §10 exists to stop minting those.

**The mechanism is already built.** §5's `revoked_at` retires a slot without deleting
anything, and the key stops resolving at `/auth/mediamtx` and `/auth/mqtt/*` the moment
it is set, because both hooks ask the `streams` table live on every connection rather
than holding a roster (§4). Nothing needs inventing. What changes is *who* sets it and
*when*: today a user clicks Retire, and under §10 the orchestrator does it as part of
tearing the flight down, on either timer.

Four properties it has to have, three of which the codebase already establishes
elsewhere:

- **Revoke before freeing capacity, never after.** The order is not cosmetic. A key that
  is still valid while its cell has been returned to the pool can be reconnected to, and
  the flight it opens lands on a cell that no longer expects it.
- **Idempotent, and never overwritten.** Both timers can fire, and shutdown can arrive
  on top of either. `revoked_at` takes the same rule §5 already gives `end_time`: the
  first timestamp is the true one, and a later teardown for a flight already closed
  changes nothing.
- **Nothing is deleted.** §5's rule holds unchanged — the flights, their alerts and
  their recordings all survive revocation, because history is the point of recording
  them. Only the credential stops working.
- **The portal has to show it.** A slot that has silently stopped working is worse than
  one that is gone: the operator retypes a URL that will never authenticate and has
  nothing on screen explaining why. An expired key should read as expired, with minting
  the next one as the obvious action.

A missed revocation is not a lost flight — the drone would reconnect, authenticate on a
key that still resolves, and open a new flight, which is semantically what happened. It
is a *security* regression rather than an availability one, and that is exactly why it
needs stating: the failure is invisible from the outside, and the thing quietly lost is
the property this whole section was written to gain.

### 10.3 What is provisioned on demand, and what is kept warm

Minting a key does two things: **assign a media cell** from the warm pool, and **spawn
the GPU container**. They look symmetrical and are not.

> **Provision on demand the thing whose wait already exists. Keep warm the thing that
> is currently instant.**

**The GPU container is provisioned on demand**, and this *moves* a wait rather than
adding one. Today the sequence is: drone takes off, publishes, `runOnAvailable` fires,
the orchestrator creates a Job, the node pool has no GPU, the cluster autoscaler
provisions a machine — one to five minutes during which the drone is airborne and
nothing is processing its video. §6 notes that a container which fails to start closes
the flight row immediately, which is to say **a capacity failure is currently
discovered by a drone already in the air**. Minting the key first turns that into a
spinner and, if there is genuinely no capacity, an honest refusal before takeoff.

Two supporting facts, both already true: `StreamVideoReader` **reconnects
indefinitely, idling until the drone starts publishing** — so a container started
early costs nothing but time and needs no change. And §4's caution that "auth and spawn
are separate events" dissolves, because an explicit human action is a better spawn
trigger than a connection attempt that may be aborted and retried.

**The media cell is not provisioned on demand.** A new cell needs a cloud load
balancer — minutes to provision, unreliable when it is not, and a standing monthly
cost. That wait does not exist today, so creating it would put a cloud API call
between an operator and a takeoff. Cells come from a warm pool instead (§10.4).

The two are not fully independent: the cell must be chosen before the container can be
configured, since the container is told which host to read from and publish to. Same
trigger, sequential rather than parallel.

`runOnAvailable`/`runOnUnavailable` do not disappear — the flight row still opens and
closes on them — but they stop *creating* things. A duplicate hook can then no longer
cause a duplicate spawn, which removes the sharpest edge from the most delicate logic
in `flights.py`.

### 10.4 The cell, and the growth that ends it

A shard is not a MediaMTX. It is a **cell**: MediaMTX, the recorder sidecar and
Mosquitto in one pod, behind one address, as one failure domain.

The recorder is already there, for the `ReadWriteOnce` reason in §2. Mosquitto joins it
for three reasons that all follow from decisions already made. §2 established that
MediaMTX needs a **single** mixed-protocol Service because WebRTC advertises exactly
one host candidate — so adding 8883 to an address that already carries RTMPS, RTSPS and
WebRTC **costs nothing**, and saves a separate load balancer for the broker. Failure
domains align: a cell dying takes video and telemetry for its own flights, rather than
a shared broker taking telemetry from every flight at once. And the scaling policy gets
one concept instead of three.

**Mosquitto's floor is two, not one**, whichever model is in use. Telemetry is not
decoration: `danger_detection` feeds it into `FrameTelemetryCombiner`, and §9's own
falsification showed that when telemetry does not arrive at rate, *every frame goes
unmatched* while every other assertion stays green. A single broker is a silent global
failure domain for the primary product mode. Note also that **Mosquitto has no
clustering** — two brokers means two independent brokers with flights assigned to one
or the other, the same sharding model as MediaMTX, not an HA pair. EMQX or VerneMQ
cluster if that ever becomes worth paying for.

#### When the cell stops being right

The cell over-provisions Mosquitto on purpose: one broker serves far more flights than
one MediaMTX, so pairing them 1:1 buys brokers nobody needs. That is the correct trade
now and the wrong one later, and the crossover is arithmetic rather than taste:

```text
cell:         N × (an unneeded Mosquitto container)   ≈ N × $5/mo
independent:  M × (an extra load balancer + cert)     ≈ M × $20/mo

break-even at roughly N = 4M
```

With two brokers, the cell is cheaper below about **eight media cells** — which is on
the order of eighty concurrent flights, and therefore eighty GPUs. Past that, the
wasted brokers outgrow the extra addresses and the tiers should be split: MediaMTX and
Mosquitto scaled on their own capacities, each with its own policy, because they do not
saturate at anything like the same load.

**This is deliberately a cheap migration**, which is why it can be deferred without
being designed for now. What changes is whether a flight's injected broker host equals
its media host. One environment variable, and the assignment logic that fills it.

### 10.5 Headroom, not thresholds

Capacity is added **ahead of demand**, never in response to a request that is already
waiting, and the trigger is derived rather than picked:

```text
headroom needed = peak arrival rate × provisioning time
```

A cell that takes five minutes to come up, against a peak of one new flight per minute,
needs five flights of slack — so a ten-flight cell scales at 50%, not at 80%. Both
inputs are measurable, and neither is a preference.

**Scale-down is real and needs hysteresis.** Scaling up takes minutes; scaling down is
instant, so symmetric thresholds flap. Scale up at 50%, down at perhaps 25%, with a
cooldown in tens of minutes.

Draining needs no migration: flag the cell as no longer accepting assignments and wait.
**Ephemeral keys are what make this bounded** — under permanent keys a cell could hold
occupied paths indefinitely, so there was no point at which removal was safe. Now the
two timers cap how long the last flight on a cell can survive, so a drained cell empties
within a known window.

### 10.6 The viewer cap makes capacity a fixed number

A flight admits a small fixed number of concurrent viewers — **two** is the working
value: one owner, one collaborator. Three simultaneous viewers on one account is
account sharing, not a use case.

The point is not policing. It is that an uncapped viewer count is the **only variable
term** in a media cell's load:

```text
per flight, fixed:      drone → MediaMTX            1 flow in
                        MediaMTX → app              1 flow out
                        app → MediaMTX              1 flow in
per flight, capped:     MediaMTX → viewers          2 flows out
                                                  ───────────────
                                                    5 flows, fixed
```

Cell capacity becomes `total flows ÷ 5`, and §10.5's policy becomes arithmetic. WebRTC
is per-peer DTLS-SRTP rather than a multicast fan-out, so every viewer genuinely costs
its own encryption — this is not a bookkeeping convenience.

**The cap is enforced in `/auth/mediamtx`, not in `mediamtx.yaml`.** The hook already
fires on every read; §4's four legitimate combinations already contain the row this
extends. A per-path limit in media-server configuration would be static config for a
per-tenant policy — the defect §4 rejects in `authInternalUsers` and again in
Mosquitto's dynamic-security plugin. As a db-writer decision, a plan with a different
limit is a column rather than a config regeneration across every cell.

Counting concurrency needs state that db-writer deliberately does not hold. **Query
MediaMTX's API** for the path's current readers rather than keeping a Redis counter:
the counter drifts when a decrement is missed, and a leaked slot locks an owner out of
their own stream, while the API is the truth by construction. Read-auth is once per
WHEP session, not per frame, so the hop is affordable.

Three details decide whether this works in practice:

- **Reconnect churn is the classic failure.** A viewer moving from wifi to cellular
  reconnects before the old connection is reaped and is refused as their own third
  viewer. Count by the token's `sub` rather than by connection, or reap aggressively.
- **This denial must be explicit, unlike every other one.** §4 requires that the reason
  is logged and never returned, because a caller learning why they were refused learns
  about another tenant. Here the refusal goes to the **legitimate owner**, and *"you are
  already watching on two devices"* is something they need told. Refusing a stranger
  stays opaque; refusing the account holder's third device does not.
- **The app's read never counts.** It reads `in/<key>` while viewers read
  `out/<public_uuid>` — separate paths in the regex config. That separation exists for
  credential reasons (§3) and happens to make this clean.

Flow count is fixed; **bytes are not**. A 4K drone is roughly four times a 1080p one
over the same five flows. Two of the five are ours (the annotated republish) and three
are not, so mixed input resolutions would return capacity to being measured in bits.

### 10.7 The session owns the media path, not the flight

A sortie is a flight. Landing to swap a battery brings the drone down and puts it back
up, so it is two flights however short the interval, and both the `flights` rows and
the recordings must say so. The grace window exists to stop a few seconds of lost radio
being misread as a landing — that is all it is for, and it must never be wide enough to
swallow a swap (§10.2).

That ruling settles the semantics and breaks a structural assumption. §5 gives every
flight its own `public_uuid` and `output_path`, but the container is handed one output
path when it spawns and has no way to learn another mid-life. One key spanning several
sorties therefore cannot give each of them its own path.

**The session becomes the row that owns the key, the `public_uuid`, the output path and
the container. Flights are intervals inside it.**

```text
User 1 ──<N Stream 1 ──<N Session 1 ──<N Flight 1 ──<N Alert
                                    └──<N Recording (via the flight it falls in)
```

The alternative — a fresh `public_uuid` per sortie — keeps the recording join trivial
and costs far more: a control channel into a running container and a rebuild of its
output connection between sorties. The whole appeal of §10.3 is a container that is
configured once at spawn and told nothing afterwards.

Three consequences, and the third is the one that decided it:

- **`record_upload` must resolve by path *and* time.** Today it is
  `filter_by(public_uuid=...).first()` — path alone, with no ordering. Two sorties
  sharing a path would silently attribute every one of the second's segments to the
  first's flight row. The fix needs no new data on the wire:
  `recordPath: /recordings/%path/%Y-%m-%d_%H-%M-%S-%f` already embeds the segment's
  start time in the `segment_path` the recorder posts, so the segment resolves to the
  flight whose interval contains it. Since no recording spans two flights (§10.2), that
  interval is unambiguous.

  Two details decide whether that actually works.

  **Parse the timestamp once, into a column.** The filename format is defined in
  `mediamtx.yaml` and read in Python, which is a coupling across two files in different
  languages — the kind that breaks silently when someone tunes `recordPath`. The
  recorder already carries `_PUBLIC_UUID_RE` for exactly this reason; it gains a
  timestamp group, and `recordings` gains a `segment_started_at` column. Resolution
  then joins on a real datetime instead of doing string surgery per query, and the
  format is depended upon in one place that can be tested directly. `uploaded_at` is
  **not** a substitute: it is when the upload finished, which after a storage outage
  and its backlog can be hours after the flight it belongs to.

  **`end_time` must be stamped at grace expiry, not at teardown.** Today those are one
  event, so the distinction has never mattered. Under §10 they separate by minutes, and
  a flight left open until its container is reaped would still be open while the *next*
  sortie is flying — two overlapping intervals, and a segment falling in both. The
  resolution is only unambiguous if flight intervals are disjoint, which means the
  flight closes when the grace window expires and the container's own lifetime is
  tracked separately.
- **The auth hook's path check moves from flight to session.** A token naming a flight
  is checked against the session that owns the path, rather than against a `public_uuid`
  the flight holds directly.
- **The viewer token becomes session-scoped**, and this is the argument that settles the
  choice rather than merely supporting it. A flight-scoped token is invalidated by every
  battery swap, so anyone watching would have their stream die and have to reload the
  page at each one — while the flight rows underneath still record each sortie honestly.
  The session is the unit a viewer cares about; the flight is the unit the archive cares
  about. Conflating them serves neither.

### 10.8 What this leaves open

- **Key creation now spends money. [open]** §3 already notes that open registration
  connects an anonymous signup to GPU spend, with `MAX_STREAMS_PER_USER` as the brake.
  That cap bounds concurrency, not churn — mint, let expire, mint again. Key creation
  needs a rate limit of its own, in the Redis the portal already uses for `/login` and
  `/register` (§4).
- **Nobody has measured a cell's capacity. [open]** Every number in §10.5 and §10.6
  depends on what one MediaMTX actually holds, and the figure has never been measured —
  including the standing claim that the GPU tier saturates first. Publishing N
  synthetic streams into one instance with viewers attached, and finding where frames
  start dropping, is the prerequisite that turns this section from a sketch into a
  configuration. **It is the first thing to do here.**
- **Does the drone controller persist the ingest URL? [open]** Not a question about
  this codebase. If a controller remembers the URL between flights, per-flight keys
  cost a transcription the current design does not; if it does not, they cost nothing
  the operator was not already paying. The design above is deliberately robust either
  way, but the answer changes how hard §10.2's timers should work to avoid a re-mint.
  Same shape as the standing question about whether any real drone needs the plaintext
  fallback (§9): a fact somebody who owns the aircraft can supply in a sentence.

---

## 11. The configuration plane **[designed]**

**Nothing here is built either.** §11 depends on §10 and is meaningless without it: the
whole of it follows from a human being present at the moment a flight is provisioned.

### 11.1 The environment has three halves, not two

`AppSettings` states its own split, and the split is one short:

> **Deployment settings** an operator sets once (model thresholds, drone optics,
> service hostnames). These are configured on the orchestrator and forwarded to every
> flight container unchanged.
>
> **Flight identity** — `FLIGHT_ID`, `PUBLISHER_TOKEN` and the two stream paths.

`build_flight_env` injects exactly five values, and all five are identity. There has
never been a place to put **per-flight configuration**, because until §10 there was
never anybody to ask: the container was spawned by `runOnAvailable`, a machine event
with no human in the loop, so everything a user might have an opinion about had to be
baked into the deployment before the drone took off.

This is not only a missing feature. Look at what is deployment-wide today:

```text
DRONE_TRUE_FOCAL_LEN_MM = 12.29    one specific airframe, hardcoded
DRONE_SENSOR_WIDTH_MM   = 17.35    "standard for 4/3 CMOS sensor"
geofencing_vertexes                ONE polygon, shared by every tenant
APP_ENV_APP_MODE                   one product per deployment
dem/dem.tif                        one raster on disk
```

**The geofence is a defect rather than a limitation.** Every tenant on a deployment is
evaluated against the same polygon, so tenant B's flight is checked against tenant A's
boundary. Nothing leaks — the app is per-flight and sole-occupant, which is what §4's
tenancy table asserts — but at least one of them gets wrong danger calls on their own
land. The honest description: **this system is multi-tenant in its credentials and
single-tenant in its configuration**, and the second half went unnoticed because there
was nowhere to put per-tenant configuration even if somebody had wanted to.

`APP_MODE` is the same shape and costs more. One deployment serves one product, so a
livestock customer and a terrain customer cannot share a cluster.

#### What stays in the environment

Worth writing down before the migration starts, because the temptation is to move
everything:

> **Does the user have an opinion about it, and would a wrong value be their mistake?**

Camera optics, geofence, DEM, mode — yes, those move. `MAX_SIZE_DETECTION_IN`, queue
timeouts, `LOG_LEVEL`, model thresholds — no. Those are deployment tuning, and putting
them in the database hands tenants a way to misconfigure a pipeline they cannot debug.

### 11.2 A "drone" is a named camera profile

The user names a camera configuration — focal length, sensor dimensions in millimetres
and pixels — and picks one when minting a key. The portal calls it a drone because that
is what a person calls it.

**§5's rule survives intact, and this is worth being precise about rather than waving
at.** §5 says a stream is a concurrency slot and *nothing in the schema models a
physical drone*; §3 says a `streams` row identifies no airframe. Both stay true. Two
users flying the same model hold two independent rows with identical values and no
knowledge of each other, and a transfer is one row deleted and another created. No
identity is tracked, because none is needed — the pipeline wants five numbers, not a
serial number.

The rule that must not bend is the one §3 states for every other identifier: **a
`drone_id` is never a credential.** It arrives in a request as a guess; ownership comes
from the session claim, and another tenant's id gets the same 404 as one that does not
exist, exactly as `stream_id` does today.

**The values are snapshotted onto the session, not referenced from it.** A foreign key
would let a later correction rewrite history: a user who fixes a wrong focal length
would make every past alert appear to have been computed with a parameter it never saw.
Copying five floats costs nothing and means an alert can always be explained by the
numbers that actually produced it — the same reason an invoice records a price rather
than pointing at the product's current one.

The existing cross-field check that physical and pixel aspect ratios agree
(`_validate_all`) becomes a check at profile-creation time, where the user can see it.

### 11.3 DEM and geofence are independent, and both optional

They are not two views of one area. A single elevation raster can carry many operating
polygons, and a boundary can move within terrain that does not. So they are two
separate selections at key creation, each skippable: a flight may use both, either, or
neither, and `open_dem_tifs()` returning `None` is already a supported degraded mode
(§9).

**Geofence validation splits in two, and the halves are not duplicates.**

The portal authors it: two numeric inputs per vertex, a minimum of three, and more on
request. That is a better instrument than a text box, and it removes the need for the
string parser in `app_settings.py` entirely — structured input never needs a regex.

The app keeps a **cheap assertion** at its boundary: at least three points, coordinates
in range. Not the parser, and not politeness. The app receives this through an
environment variable, and what fills that variable can be wrong for reasons that have
nothing to do with the user — a bad migration, a defect in composition, a container run
by hand while debugging. A malformed geofence that is silently accepted produces wrong
danger calls on somebody's land, which is the one failure this pipeline must not have.
The same position is already taken twice in this document: §4's unrecognised actions
arrive closed, and db-writer enforces bcrypt's length bound even though the portal
could have.

### 11.4 Getting the raster into the container without changing the app

The app reads its DEM from a mounted path. **That interface does not change**, and
everything below follows from keeping it.

The obvious answer — one shared volume holding every tenant's rasters — is wrong twice.
It needs `ReadWriteMany`, which §2 already rejected for the recorder as "a paid,
network-attached filesystem standing in for what is a handoff between two processes."
And it would put every tenant's terrain inside every flight container, which is the
tenancy hole §11.1 exists to close.

Instead, **something outside the app container puts the file where the app expects it**:

```text
Kubernetes   initContainer holds a short-lived, single-object URL, writes the
             raster into an emptyDir; the app container mounts that emptyDir at
             the path it already reads

Docker       the orchestrator fetches into a per-flight directory and bind-mounts
             it, which is the same shape with the same property
```

The app changes by **zero lines** — it still opens a file. The storage credential never
enters the container that processes untrusted video, which is the property §3 protects
when it says the GPU tier holds no reusable credential. And the fetch happens inside the
15-minute pre-flight window (§10.2), which is the only reason a hundred-megabyte
download is affordable at all: under spawn-on-stream-appearance the same transfer would
have run with the drone already publishing, and the opening minutes of every flight
would have been lost to it.

That the two platforms do this differently is not a wrinkle. §2 already records three
settings that are genuinely per-backend rather than shared, and `FlightRuntime` exists
precisely so `flights.py` never learns which one is underneath.

**The later optimisation, deliberately not the starting point.** `extract_dem_window`
reads windows rather than whole rasters, so a **Cloud Optimized GeoTIFF** read over
GDAL's `/vsiaz/` or `/vsis3/` would fetch only the tiles a flight touches and remove the
download entirely. It is the right answer if transfer time ever becomes the constraint,
and the wrong one to reach for now: it puts storage access back inside the app
container, which is exactly what the init container was for.

### 11.5 One storage account, tenants separated by prefix

**Not one account per user.** Holding a customer's own cloud credentials would mean
encrypting them at rest, rotating them, and a breach that hands out other people's
storage accounts rather than only this system's data. The deployment owns one account
and tenants are separated by key prefix:

```text
tenants/<user_id>/dems/<uuid>.tif
tenants/<user_id>/recordings/<public_uuid>/<timestamp>.mp4
```

This generalises what §9 already carries as an open item — recorder per-tenant upload
prefixes — rather than inventing a second scheme beside it. Nothing tenant-specific goes
in `users`; the prefix is derived from `user_id`.

**db-writer holds the account key and mints scoped, short-lived URLs**: a single-object
`PUT` when a user uploads a raster, a single-object `GET` when a flight is provisioned.
Three reasons, and one honest cost.

- It is already the authority that decides who may reach what, and a credential belongs
  with the decision it enforces. A separate storage service would need the same
  ownership data and would either duplicate those checks or call db-writer anyway.
- It is internal-only by §8 and never reachable from a browser, which the portal is not.
- It already holds `SESSION_JWT_SECRET`, so it is the tier already hardened for
  secret-bearing.

The cost is concentration: a compromised db-writer now also yields storage access. That
is real and it is small, because a compromised db-writer already yields the signing key
for every credential in the system — the storage key does not meaningfully widen a blast
radius that size.

The recorder keeps its own credentials rather than being migrated onto minted URLs. It
only ever writes, only to a prefix it derives, and never reads — and it is a sidecar in
the MediaMTX pod, a different trust position from the tier that answers tenant requests.
Moving it would be tidiness rather than a security gain.

**Pre-signed URLs are used here and refused in §9, and the distinction is the point.**
§9 rejects them for alert images because those would be handed to a *browser*, becoming
bearer credentials that bypass the alert/flight/caller check for their lifetime. Here
the holder is this system's own init container, the grant is one object belonging to the
tenant the flight already belongs to, and the lifetime is minutes. Same mechanism,
different holder — recorded explicitly so the two do not read as an inconsistency
somebody later "fixes".

### 11.6 Uploaded rasters: what is checked now, and what is deferred

A tenant-supplied GeoTIFF is untrusted input parsed by GDAL, a library with a real CVE
history, and §3 already describes the GPU tier as a container processing untrusted
video. It would now also process untrusted files.

**What is done now** is deliberately basic: a size limit and a format check — the file
opens, it is a GeoTIFF, its bounds are sane — performed **on upload rather than at
flight time**. The placement is the part that matters. A file crafted against a parser
bug is far better detonating in a short-lived validator than in a container holding a
GPU and a publisher token, and the user learns their file is bad while they are on the
upload page rather than fifteen minutes later behind a spinner.

**What is deferred, and why that is defensible.** Sandboxing the parse properly, fuzzing
the path, or pinning GDAL against an advisory feed are all real work, and the blast
radius does not yet justify them: the container is per-flight and sole-occupant, so what
a successful exploit reaches first is *the attacker's own tenant data*. The
consequential risk is escape to the node, and that is not a raster problem — it is the
argument §2 already makes for the Kubernetes backend and its scoped ServiceAccount, and
it is answered there or not at all.

This is recorded as a deliberate deferral rather than left unmentioned, because the
thing that makes it defensible is an assumption that can quietly stop holding: **if a
flight container ever gains access to anything beyond its own tenant's data, this
paragraph expires.**

### 11.7 What this forces elsewhere

- **§3 and §5 are extended, not contradicted.** No airframe identity is recorded and no
  identifier becomes a credential, so both sections' rules hold verbatim. What changes
  is that the schema grows a configuration side — camera profiles, geofences, DEM
  references — beside the credential side it has today.
- **`APP_MODE` moves from the deployment to the key**, which is what turns one cluster
  serving one product into one cluster serving both. It is the single highest-value item
  in this section and the cheapest: the app already selects its pipeline from this
  variable at startup (`app/main.py`), so nothing in the app tier changes at all.
- **The geofence parser leaves `app_settings.py`** and a short range assertion replaces
  it (§11.3).
- **Object-storage tenancy stops being a recorder-only question** and needs deciding
  once, for both rasters and recordings (§11.5).
- **Per-tenant configuration needs the same tenancy tests the credential side already
  has [open].** §9's flight-history work was checked by *breaking* it — deleting the
  `user_id` filter and confirming 10 assertions fail. Nothing here is trustworthy until
  the equivalent exists: another tenant's camera profile, geofence and DEM must all be
  unreachable, and the test must be shown to fail when the filter is removed.
