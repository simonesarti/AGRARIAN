# Cloud Architecture — Drone Monitoring Service

**Status:** target architecture for the `secure-cloud` branch.
**Supersedes:** the previous per-session-isolated-stack document, which described a
different design (one MediaMTX, Mosquitto, ws-server and Traefik *per user session*)
and is no longer the direction. Nothing in that document should be treated as current.

**How to read this.** Sections 1–8 describe the system as it runs today; anything in
them that is *not* built says so in place. Section 9 is the current state — what backs
each claim, known weaknesses, open questions, and what to do next. Sections 10 and 11
are decided direction rather than description: 10 is unbuilt, 11 is mostly built and
carries its own split at the top.

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

### Platform: Kubernetes

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

#### Migration is mechanical, with two real changes

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

**Two kustomize defects were found by deploying, and neither is visible by reading.**
Both are recorded here rather than in a changelog because both would recur for anyone
editing `configs/kustomization.yaml`:

- **The `namespace:` transformer rewrites `Namespace` objects themselves**, collapsing
  `agrarian-flights` into `agrarian` — which would have given the orchestrator's Role
  authority over Jobs in the namespace holding db-writer, Redis and Mosquitto, undoing
  the entire argument of `orchestrator-rbac.yaml`. kustomize reports it as an "ID
  conflict", which does not sound like what it is. There is now no global namespace
  transformer; every manifest names its own.
- **A `configMapGenerator` without an explicit namespace generates into `default` *and*
  silently declines to stamp its content hash into the references.** The build succeeds
  and every mount dangles, surfacing only as pods stuck in `ContainerCreating`.

Kubernetes does **not** reverse-proxy anything itself: `Ingress` and the Gateway API are
interfaces, and a controller has to be installed to implement them. That controller is
Traefik here, and it is the same Traefik the compose stack already runs, which is why the
routing config survives the migration rather than being rewritten into a vendor's
annotations. Nothing in the design depends on Traefik specifically — see §7.

Managed Kubernetes also brings cert-manager, which is what closes the TLS item in §9 for
all three terminators at once: Traefik for the HTTP family, and Secrets mounted by
MediaMTX and Mosquitto for the protocols they terminate themselves.

#### Build the orchestrator against an interface, not a cluster

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

##### The service account is the point

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

### Stream keys

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

### Viewer tokens

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

### Publisher tokens

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

### Session tokens

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

### Registration is open

Anyone may create an account. The consequence to keep in view: an account can mint stream
keys, and a stream key is the thing that causes a GPU container to be created. Open
registration therefore connects an anonymous signup to GPU spend, and the limit on that
is **concurrent flights per user**; `MAX_STREAMS_PER_USER` bounds concurrency but
not duration, which is the quota question in §9.

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

| Component | Instances | Tenancy mechanism |
| --- | --- | --- |
| **GPU app** | **One per active flight** | Sole occupant — no internal tenancy needed |
| MediaMTX | Shared, replicated on load | Regex paths + HTTP auth hook |
| Mosquitto | Shared, replicated on load | Per-stream credentials + topic ACLs |
| ws-server | Shared, replicated on load | Per-flight JWT (view + publish scopes); Redis pub/sub fan-out |
| db-writer | Shared, replicated on load | Stateless per request; bcrypt user auth |
| Redis | Shared | Channel per flight (`flight:{id}`); rate-limit counters on db 1 |
| Recorder | Shared | Segment → flight_id resolved via `recordings` table; `tenants/<user_id>/…` key prefix |
| Orchestrator | Shared | Spawns/stops app containers |
| Portal | Shared, replicated on load | Session token → `user_id`, read from the claim not the URL |

A single MediaMTX serves every drone publishing and every viewer watching; a single
Mosquitto carries every publisher's telemetry. They are separated by path regex, credentials
and ACLs — not by having one broker each.

> **db-writer holds no per-flight state.** Every endpoint works from the `flight_id` in
> the URL plus the database, so any replica serves any flight regardless of which one
> opened it. The only process-local object is `AlertWriter` — a queue and a thread that
> keep database latency off the caller's hot path — and it is flight-agnostic, so a
> replica accepts alerts for flights it has never seen.

### ws-server

Previously broadcast every alert — including the JPEG and position — to every connected
client. Now maintains a per-flight session map, and because horizontal replicas cannot
share an in-memory client set, fan-out goes through Redis pub/sub.

Replicas **subscribe selectively** to the flights they actually have viewers for, rather
than pattern-subscribing to everything. With base64 JPEGs in the payload, pattern
subscription would ship every tenant's imagery to every replica.

#### Redis failure behaviour

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

### MediaMTX

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

**MediaMTX's own reader page cannot be used, and this is not a configuration problem.**
`GET /<path>/` answers **401 with `WWW-Authenticate: Basic` and never calls
`/auth/mediamtx` at all** — not for `?jwt=`, `?token=`, `?user=&pass=`, `Authorization:
Bearer`, or HTTP Basic. That page is gated behind the internal user roster which
`authMethod: http` replaced, so no credential this system can mint would ever open it.
The watch page therefore negotiates WHEP itself against a `<video>` element; the media
path is still browser-to-MediaMTX DTLS-SRTP and only the signalling moved. Embedding
that page again is the one thing that must never quietly come back.

**WHEP checks the content type before the credential**, so a POST without
`application/sdp` is refused before the hook is consulted — a 400 that looks like an
authorisation result and is not.

**Auth and spawn are separate events.** The auth hook fires on every connection attempt,
including aborted and retried ones. Spawning GPU containers from it would spawn them for
drones that never stream. The spawn belongs on `runOnAvailable`.

#### The image tag is load-bearing

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
`Form(...)` endpoints expect. Fixed, and covered by `run_recording_upload.sh`.

The same constraint rules out shell syntax in any hook: no pipes, no `&&`, no redirects.
`-O /dev/null` is an argument, which is why it works.

### Mosquitto

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

### Portal

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

#### The read side: flight history

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
  cacheable, and individually authorised. "Tens of megabytes" was not a figure of speech:
  this document calls these images crops, and they are not — `output_alert_streamer`
  stored the **full-resolution annotated frame**, unresized, so each one was a 1920×1080
  JPEG. They are now capped at 960 on the longest edge (`ALERTS_MAX_IMAGE_EDGE_PX`, see
  §9), which shrinks the page without changing this reasoning: fifty images is still
  fifty requests' worth of bytes to inline, and the authorisation argument never
  depended on their size at all.

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

#### Rate limiting the two anonymous endpoints

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

| Table | Key columns |
| --- | --- |
| `users` | `user_id` PK, `email`, `password` (bcrypt) |
| `streams` | `stream_id` PK, `user_id` FK, `stream_key` unique, `label`, `revoked_at` — capped per user (§4) |
| `flights` | `flight_id` PK, `stream_id` FK (NOT NULL), `public_uuid` unique, `output_path`, `end_time` |
| `alerts` | `alert_id` PK, `flight_id` FK, JPEG, dimensions, timestamps |

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

**Every step runs, end to end.** Step 6 is verified against a real GPU in both
modes, and step 7 now ends where it was always supposed to: a person signed in, pressed
Watch, and saw the annotated video play in Chrome and in Firefox. Nothing in this
lifecycle is unobserved any more — though that last step is a human's report on one
afternoon rather than an assertion, and §9 says what it does and does not cover.

1. User registers on the portal → row in `users`, and logs in for a session token.
   Registration is open to anyone. — the portal's `/register` and `/login`
   pages over db-writer's routes of the same names.
2. User adds a stream → row in `streams` with a generated `stream_key`. The portal
   offers rotate and retire. — `GET/POST /streams`, `/rotate`, `/revoke`
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
   the same auth endpoint with `action: "read"`. — the watch page fetches it
   at load; with more than one flight active the call must name a `stream_id` rather than
   being guessed for.
8. Publisher disconnects. `runOnUnavailable` → container stopped, `end_time` stamped.

**The app container never sees end-user credentials.** It used to call `/session/start`
with the user's email and password, putting a reusable account credential inside a
container that processes untrusted video. The orchestrator now opens the flight and
injects only the result — which also removes the "DB session start failed → abort the
run" coupling, since the app no longer authenticates anyone.

### A dropped stream is not usually a finished flight

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

### TLS termination

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
  clients it should prefer and admit nothing new below. Measured rather than assumed,
  in `run_ingress_tls.sh` — with an OpenSSL old enough to still offer TLS 1.1, because
  modern curl refuses to *send* a 1.1 ClientHello and an assertion driving it measures
  the client rather than the server.

#### The certificate: one leaf, three terminators

A real Let's Encrypt wildcard for `agrarianlivestock.com` and `*.agrarianlivestock.com`
sits in `certificates/server/` as `server.crt`, `server.key` and `ca.crt`. All three
terminators mount those files and none asks who signed them, which is the property that
let the whole tier be built and measured against a local CA first.

**Issuance needed none of the infrastructure it appeared to wait on.** Let's Encrypt
will not issue for an IP address, and it is easy to read that as "nothing can be issued
until a name resolves here" — which does not follow. A DNS-01 challenge proves control
of a **zone**: the CA reads a TXT record at `_acme-challenge` and never connects to the
deployment. The certificate was obtained with no cluster, no load balancer and no A
record anywhere in the zone. What is genuinely required is a domain and a DNS API
credential the issuer supports.

Four things about the handover are load-bearing:

- **It is a three-file copy.** `mosquitto.conf` names `ca.crt` as its `cafile`, so
  copying only the leaf and key leaves one terminator pointing at an intermediate
  unrelated to the certificate beside it. `require_certificate false` means nothing
  verifies against it, so the mismatch is silent — and it is not hypothetical: the
  first renewal changed the intermediate from `YR2` to `YR1`.
- **The leaf is RSA-2048 by choice.** ACME clients default to EC256, and taking that
  default would change the key algorithm underneath all three terminators in the same
  move that changes the issuer. One leaf also serves the drone-facing listeners, where
  the TLS floor is 1.2 for old ground-station software, so the weakest client governs.
- **The lifetime is 90 days, not the local CA's 397**, which turns renewal from a
  document into a dated obligation. `scripts/renew_certs.sh` does it and does the three
  different reloads below; `scripts/check_certs.sh` asks the listeners daily what they
  are actually serving, because a correct file and a stale process are indistinguishable
  from disk.
- **The ACME block in `configs/traefik/traefik.yml` stays commented out.** Under
  Kubernetes cert-manager owns issuance and writes the `agrarian-tls` Secret; under
  compose the certificate arrives as a file from outside. Traefik's own ACME store is a
  private `acme.json` it does not hand back, so it could only ever serve one of the
  three terminators. Only the mount point was ever load-bearing.

`scripts/generate_local_certs.sh` is **not retired**: every test runner that mounts the
real `mediamtx.yaml` or `mosquitto.conf` issues a throwaway leaf from it into a
temporary directory, which keeps the suites independent of whatever real certificate is
on the machine and stops a public leaf's private key reaching a test container.

#### Renewal: what each terminator does when the leaf changes

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

> -  The port constants in `app/shared/processes/constants.py` are corrected.
>   `RTMPS_PORT` and `RTSPS_PORT` now carry MediaMTX's actual defaults (1936, 8322)
>   rather than 8443 and 441, and `HTTPS_PORT`/`WSS_PORT` are 443 rather than 8443,
>   which is what this table says Traefik terminates and what removes the collision
>   between them. `WEBSOCKET_PORT` no longer derives from `HTTPS_PORT` — it is
>   ws-server's WebSocket listener (8765) and had nothing to do with HTTPS. None of
>   these names is read by any code path today (the app reaches its services through
>   `app_settings.py`), so this was latent throughout and is now simply correct.
> -  SRT and MoQ are disabled explicitly in `mediamtx.yaml` — see §4.
> - **[note]** compose publishes ws-server's alert-write API on host `8001` and db-writer
>   on `8002`, which this section says must never be routed from outside. That is the
>   interim laptop-app deployment described in §7, not the target topology.

---

## 9. Where things stand

Sections 1–8 describe the system as it is. This section says which parts are running,
what evidence stands behind each, what is known to be wrong, and what to do next.

**It is not a changelog.** How a thing came to be built is in git; what is true now is
here. Where a finding still teaches something — a trap that will catch the next person —
it lives in the section it belongs to rather than in a list of past work.

### What backs each claim

Tests are in `tests/comms/`, with a README covering what each guards. The shell runners
stand up their own containers and clean up after themselves; the `test_*.py` files need
no stack. Every runner that mounts the real `mediamtx.yaml` or `mosquitto.conf` issues a
throwaway certificate first, because both terminate their own TLS and MediaMTX exits at
startup without one.

| Capability | Evidence |
| --- | --- |
| Account registration, login, session tokens | `test_schema.py`, `test_session_tokens.py`, `run_portal_auth.sh` — against real PostgreSQL, two replicas |
| Stream slot CRUD, the per-user cap | `test_schema.py`, `run_portal_auth.sh` — cap proven under 20 simultaneous adds across two replicas |
| MediaMTX authorization, publish and read | `test_mediamtx_auth.py`, `run_mediamtx_auth.sh` — real publishes and HLS reads, two tenants |
| Mosquitto authorization and telemetry isolation | `test_mqtt_auth.py`, `run_mqtt_auth.sh` |
| Per-flight publisher tokens on every write path | `test_tokens.py`, `test_tenancy.py` |
| ws-server per-flight isolation, Redis fan-out | `test_tenancy.py`, `test_replicas.py`, `run_redis_failure.sh` |
| db-writer replica safety | `run_db_replication.sh` — two replicas, real PostgreSQL |
| Flight lifecycle, reconnect, crash recovery | `test_orchestrator.py`, `run_orchestrator.sh`, `run_orchestrator_recovery.sh` |
| The GPU app in both modes, driven by the orchestrator | `run_orchestrator_real_app.sh` — real GPU |
| Portal pages, flight history, alert paging | `run_portal.sh` (158), `test_flight_history.py`, `test_alert_paging.py` |
| Per-slot mode, geofence, camera profile | `test_app_mode.py`, `test_geofence.py`, `test_camera.py`, `run_portal.sh` |
| Rate limiting, public and internal | `run_portal.sh`; db-writer's own `/login` driven against real Redis |
| Ingress TLS on all three terminators | `run_traefik_tls.sh`, `run_ingress_tls.sh` |
| Certificate renewal and reload behaviour | `run_cert_renewal.sh`, plus one real forced renewal |
| Recording upload and per-tenant prefixes | `run_recording_upload.sh` |
| Kubernetes `FlightRuntime` and the hub manifests | `run_k8s_runtime.sh`, `run_hub_manifests.sh` — k3s in a container |
| One media cell's capacity | `run_media_capacity.sh` — 48 concurrent flights, no degradation |

Two claims rest on **human observation rather than assertion**, and nothing re-checks
either: a person watched the annotated video play in Chrome and Firefox, and every page
was looked at in a browser at desktop and phone width. A change to `watch.js` or the
stylesheet can break both silently.

**The habit that matters more than any count above:** properties that could pass
vacuously are checked by breaking them. Cursor paging is proven by an `OFFSET` control
that repeats a row; the stream cap by removing the row lock and watching it overshoot;
tenancy by deleting the `user_id` filter and confirming the assertions fail; `/dev/shm`
sizing by a control pod that gets 64 MB. A test that cannot fail is not evidence, and
several here were vacuous until that was checked.

### What to do next

Two external asks, neither of which is work and both of which gate everything after
them:

1. **A DEM raster** (`dem/dem.tif`, `dem/dem_mask.tif`). Without it `danger_detection`
   runs with slope and no-data analysis skipped. The code is not the unknown — it ran
   against real elevation data during the app's development — but nothing here
   exercises it, and no automated coverage can exist until a raster does.
2. **Azure GPU vCPU quota.** Reviewed rather than granted on request, so it is days.
   Everything else Azure-shaped is an afternoon once a cluster exists.

Then, in order:

3. **Decide `MEDIA_HTTP_PUBLIC_HOST`** before the first cluster flight. Either both
   LoadBalancer Services share one address (leave it unset) or it names Traefik's.
   Getting it wrong fails as WHEP answering 201 with a black player.
4. **Push five images to a registry**, fill `configs/k8s/endpoints.env`, and choose a
   storage class. None of this can be done before a cluster exists.
5. **Measure a cell properly**, from more than one machine and with WebRTC viewers.
   The floor is 48 flights; the ceiling is unknown.

### Known weaknesses

Live on this branch right now, as distinct from work not yet started.

- **The DEM is absent everywhere here, so the geo stage runs degraded.**
  `open_dem_tifs()` returns `None` and slope and no-data analysis are skipped;
  geofencing and the safety radius still run. The harness reports which of the two it
  got, so a green run is never mistaken for full geo coverage. What is missing is a
  raster and then the regression coverage that needs one.
- **The orchestrator holds the Docker socket under the backend this repo runs.**
  Anything reaching its port can start containers on the host. The alternative is built
  — `FLIGHT_RUNTIME=kubernetes` with a ServiceAccount scoped to Jobs in one namespace —
  but what is deployed here is still compose, and having the fix is not running it.
- **Nothing re-checks the browser observations.** See above.

### Open questions

Grouped by what each is waiting for, because "open" has meant several different things
in this document and the difference decides whether anything should be done.

**Waiting on a measurement or a complaint** — building these now would be speculative:

- **MediaMTX cannot be scaled by a load balancer.** A path lives on the instance its
  publisher connected to, so a viewer must reach *that* one. The options are path-aware
  routing, a relay tree, or sharding by path. Not urgent, and now for a measured
  reason: one instance carried 48 concurrent flights while a GPU carries one.
- **ICE-TCP does not cover a network that permits only 443.** TURN over 443, or HLS
  proxied through the ingress tier, are the answers. Neither is worth building before a
  real viewer reports being unable to watch.
- **Auth-endpoint caching.** Every publish and read costs one indexed lookup. A cache
  delays revocation of a credential that has no expiry, so: replicas first, cache only
  if measurement demands it.

**Waiting on a feature that does not exist yet:**

- **No email verification**, and no password reset. Both land together when either is
  needed.
- **Quota and billing.** `MAX_STREAMS_PER_USER` bounds concurrency, not duration or
  total GPU hours. Ten slots flying all day is within the cap.
- **A recording is a location, not a download.** Handing the segment over means the
  portal holding storage credentials — which §7 keeps out of the internet-facing tier —
  or db-writer minting pre-signed URLs. A real decision about which service owns storage
  credentials, not a history feature.

**Deliberate trades, recorded so they are not mistaken for oversights:**

- **Signing out drops the cookie; it does not revoke the token.** The price of a
  stateless session, and why the lifetime is hours. A deny-list would put a server-side
  lookup back on every request.
- **TLS is optional on the drone side, not compulsory.** Every encrypted listener
  exists and the portal prints the encrypted URL; the plaintext ones remain as the
  fallback §7 describes. Turning `optional` into `strict` is a two-line change, gated
  on knowing whether any drone that will actually fly needs it.
- **MediaMTX upgrades are now manual**, because the image is pinned. That is what stops
  a default changing underneath the stack, and it costs a person noticing security
  fixes.

**Genuine gaps with no blocker but nobody's hand up:**

- **Mosquitto's `SIGHUP` on renewal has no answer under Kubernetes.** cert-manager and
  the kubelet cover Traefik and MediaMTX there; nothing signals Mosquitto.
  `scripts/renew_certs.sh` handles it under compose only.
- **The DEM half of per-tenant configuration is untested**, because §11.4's per-tenant
  raster is not built and there is no ownership path to break.

---

## 10. Ephemeral keys and elastic media capacity **[designed]**

This is a decided direction, recorded here because it changes three things §3, §5 and
§6 currently state as settled: the stream key stops being persistent, the GPU container
stops being spawned by the media server, and MediaMTX stops being a single instance.
Sections above describe what runs today and remain accurate as such.

**Almost none of it is built**, and the two exceptions are settings rather than
structure: `RECONNECT_GRACE_S` is 120 s and `recordSegmentDuration` is 24 h, both for
reasons §10.2 gives. Everything else below is a decision, not a description.

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
- **A cell's capacity has a floor but no ceiling.** `run_media_capacity.sh` carried
  **48 concurrent flights — 240 flows on the model above — with nothing degrading**:
  every reader received every byte, MediaMTX logged no warning, and it used about a
  ninth of one 24-core host for a gigabit of combined throughput. That settles the
  claim the GPU tier saturates first, since 48 flights is 48 GPUs.

  The number itself is still open, from both directions. The ramp stopped because the
  *load generator* ran out of host, not because MediaMTX did, so 48 is a floor. And it
  is an upper bound on the real thing — RTMP readers rather than WebRTC, no TLS, one
  loopback bridge — so the operating figure is lower than whatever the true ceiling is.
  Closing this needs load from more than one machine, with viewers paying for their own
  DTLS-SRTP.
- **Whether the drone controller persists the ingest URL is moot.** It was
  asked because per-flight keys would cost a transcription that permanent keys did not,
  *if* the controller remembered the old URL. It does not matter: under §10.1 the key is
  minted per flight, so the operator visits the portal before every takeoff regardless.
  A controller that remembers last week's URL remembers a dead one.

  Closing it leaves one thing behind, and it is a UX consequence rather than a design
  question. **A stale persisted URL becomes a normal operator mistake**, where today it
  simply keeps working. The operator's muscle memory — set it once, press go — stops
  being correct, and §4 deliberately returns no reason for a refused publish, because a
  caller learning why it was refused learns about another tenant.

  That rule was written for a stranger probing stream keys, and it is right for one. It
  is wrong for the account holder publishing on their own expired key, who gets silence.
  The same distinction §10.6 draws for the viewer cap applies here: refusing a stranger
  stays opaque, refusing the owner should not. The portal knowing that a slot's key was
  presented and refused — and saying so on the page — is the cheap version, and it wants
  designing alongside the two timers rather than after somebody is standing in a field
  wondering why nothing happens.

---

## 11. Per-slot configuration — mostly built

**Read the split first, because it is not what the section order suggests.**

| | |
| --- | --- |
| **Built** | `APP_MODE`, geofence and camera profile, all per stream slot; named and reusable rows the user owns; snapshotted onto the flight |
| **Designed** | the DEM only — §11.4, §11.5, §11.6 — which needs object storage rather than another column |

The built half was expected to wait on §10 and did not have to. It needs only that
db-writer can resolve a stream key to its owner when a flight opens, which it always
could — so per-slot configuration landed without a human being present at provisioning
time, which is the thing §10 would add. What §10 changes is choosing *per flight*
rather than per slot.

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

**The geofence was a defect rather than a limitation, and it is now fixed. [built]**
Every tenant on a deployment was evaluated against the same polygon, so tenant B's
flight was checked against tenant A's boundary. Nothing leaked — the app is per-flight
and sole-occupant, which is what §4's tenancy table asserts — but at least one of them
got wrong danger calls on their own land. The honest description of the whole class:
**this system is multi-tenant in its credentials and single-tenant in its
configuration**, and the second half went unnoticed because there was nowhere to put
per-tenant configuration even if somebody had wanted to.

It was latent rather than live, because `geofencing_vertexes` defaults to `None` and
nothing was wrong while nobody set one. That is the reason it was worth fixing before
the combination arrived rather than after: the failure mode is a *wrong answer*, not an
error, so the first sign of it would have been a tenant disputing an alert.

`APP_MODE` was the same shape and cost more: one deployment served one product, so a
livestock customer and a terrain customer could not share a cluster. **It is the one
item in this section that is now built** — see §11.7. The rest of the list above still
stands.

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

**The values are snapshotted onto the flight, not referenced from it. [built]** A
foreign key would let a later correction rewrite history: a user who fixes a wrong focal
length would make every past alert appear to have been computed with a parameter it
never saw. Copying five floats costs nothing and means an alert can always be explained
by the numbers that actually produced it — the same reason an invoice records a price
rather than pointing at the product's current one. It lands on `flights.camera` rather
than the session of §10, which does not exist yet; when it does, the snapshot moves with
the row that owns the configuration.

That snapshot is also what makes a profile safe to **hard delete**, which is what "I
sold that drone" has to mean. Nothing in history points at the named row, so removal
unassigns the slots still using it and every recorded flight keeps the optics it
measured with.

**The aspect-ratio cross-check moved with it.** `_validate_all` asserts that the
millimetre and pixel dimensions describe the same sensor, and that assertion now also
runs where the user types the numbers. It is the one rule here worth stating twice: a
mismatch does not fail anything downstream, it silently scales every ground measurement
on one axis, and a form error is a far better place to learn that than a flight's
output.

Verified by 41 assertions in `tests/comms/test_camera.py`. The one worth naming is the
same shape as the geofence's: **a slot naming no profile leaves the deployment's optics
standing**, which is what keeps every existing deployment behaving exactly as it did.

#### The seam is where the defects were **[built]**

All three settings are now also driven over real HTTP in `run_portal.sh` — 118
assertions to 158 — through the portal's own pages and checked against db-writer. That
section exists because the no-stack suites do not cross the boundary between services:
they exercise `db_manager` and `build_flight_env` directly, and both defects found in
this work lived in between.

The first was arity: `portal/main.py` passed five arguments to a
`DbWriterClient.create_stream` that took four, so slot creation was a 500 while every
other suite stayed green.

**The second is the one worth remembering.** `/flight/open` never forwarded
`camera_env`. `open_flight_for_key` computed it correctly and the route dropped it on
the floor, so a camera profile could be stored, listed, selected on a slot and shown as
selected — and never reach a container. Every layer was individually right.
`test_camera.py` asserts the manager returns it and cannot see that nobody asks. The
lesson is narrow and worth writing down: **a value in `open_flight_for_key`'s return is
not a value the orchestrator receives**, because that response is assembled field by
field, and the next setting added to it will have the same trap waiting.

The assertion that closes the loop is a flight opened on a fully configured slot,
checked to carry all three — mode, boundary and optics — at once. Everything else proves
the portal wrote a choice down; only that one proves it is what flies.

The existing cross-field check that physical and pixel aspect ratios agree
(`_validate_all`) becomes a check at profile-creation time, where the user can see it.

### 11.3 DEM and geofence are independent, and both optional

They are not two views of one area. A single elevation raster can carry many operating
polygons, and a boundary can move within terrain that does not. So they are two
separate selections at key creation, each skippable: a flight may use both, either, or
neither, and `open_dem_tifs()` returning `None` is already a supported degraded mode
(§9).

**Geofence validation splits in three, and the parts are not duplicates. [built]**

The **portal authors** it: two numeric inputs per vertex, always one blank row to type
into, and more on request. Longitude first, matching the app's parser and GeoJSON
rather than the "lat, lon" a map usually shows — which is why both boxes are labelled.
It validates nothing.

**db-writer owns the rules** — ranges, the three-point floor, a ceiling so a form post
cannot put an unbounded string into an environment variable — and answers 400 with a
message written for a human. Stored as JSON `[[lon, lat], ...]` and rendered into the
`"(lon, lat), ..."` spelling at injection time, so `geofence_to_env` is the one place
that knows both. Structured storage is what lets the app-side parser be retired later
without a data migration.

**Boundaries are named rows, not columns on a slot.** A `geofences` table belonging to
the user, and `streams.geofence_id` pointing at one. A boundary outlives the slot flying
it — a herd owner works the same field for years, often from several slots at once — and
re-entering the polygon per slot is how two copies of one field drift apart. Editing it
once updates every slot pointing at it, which is the point of naming it.

**What makes that safe is `flights.geofence`: a snapshot, not a reference.** The
boundary a flight was judged against is copied onto the flight when it opens. Without
that, editing a fence would silently restate what every past flight had been checked
against, and *"which boundary produced this alert?"* — the question a tenant disputing
one asks — would be answered with today's shape rather than the one that flew. It is
also what makes a boundary safe to **delete**: nothing in history points at the named
row, so removal unassigns the slots still using it and leaves every recorded flight
intact. A foreign key would have had to refuse the delete or null it, and either way the
past flight loses its answer.

The **app keeps a cheap assertion** at its boundary: at least three points, coordinates
in range. Not the parser, and not politeness. The app receives this through an
environment variable, and what fills that variable can be wrong for reasons that have
nothing to do with the user — a bad migration, a defect in composition, a container run
by hand while debugging. A malformed geofence that is silently accepted produces wrong
danger calls on somebody's land, which is the one failure this pipeline must not have.
The same position is already taken twice in this document: §4's unrecognised actions
arrive closed, and db-writer enforces bcrypt's length bound even though the portal
could have.

Verified by 43 assertions in `tests/comms/test_geofence.py`. Three are worth naming.
The rendered string is **parsed back by the app's own regex** and compared to the points
that went in, which is the only thing standing between two spellings in two services
that no single test would otherwise cross. A slot with no fence **injects nothing at
all** rather than an empty variable — asserted specifically, because `env_ignore_empty`
makes the app treat those alike *today* and that is a setting somebody could change,
while an absent variable is unambiguous. And the snapshot is checked by **moving the
boundary and then deleting it**: the next flight gets the new shape, the recorded one
still reports the old, and the deletion leaves both the recorded flight and the other
tenant's boundary untouched.

Two sequential ids are now in play — `stream_id` and `geofence_id` — and both are
matched against the session's user in the query that selects the row. A slot cannot be
pointed at another tenant's boundary, at creation or afterwards, and both refusals are
the same 404 as an id that does not exist.

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
- **`APP_MODE` has moved from the deployment to the stream slot. [built]** This is what
  turns one cluster serving one product into one cluster serving both, and it was the
  cheapest item here: the app already selects its pipeline from this variable at startup
  (`app/main.py`), and the image carries both, so **nothing in the app tier changed at
  all**.

  It landed on the *slot* rather than on the key, which is §10's shape one level up and
  needs none of §10 to work. db-writer already resolves a stream key to its stream and
  owner when the flight opens, so the identity needed to look up a preference has always
  been present — `open_flight_for_key` returns `app_mode`, `/flight/open` passes it on,
  and `build_flight_env` injects it. What §10 would add is choosing *per flight* rather
  than per slot.

  `NULL` means "follow the deployment", which is what every row created before the
  column existed means and what makes this change invisible to a single-product
  deployment. The orchestrator injects nothing in that case and `base_env` stands.

  Verified by 23 assertions in `tests/comms/test_app_mode.py`, no stack required. The
  one worth naming is not that the slot's mode wins — it is that **no preference leaves
  the deployment's setting alone**, which is the assertion protecting every deployment
  that exists today.
- **The geofence parser leaves `app_settings.py`** and a short range assertion replaces
  it (§11.3).
- **Object-storage tenancy stops being a recorder-only question** and needs deciding
  once, for both rasters and recordings (§11.5).
- **Per-tenant configuration carries the same falsification the credential side does.**

  `UserDirectory` enforces ownership with `user_id=user_id` inside the query that
  selects the row, in **ten places** across geofences and camera profiles. Each was
  removed and the suites re-run, one path at a time rather than all at once — because
  stripping all ten makes the first breach destroy the fixtures the later assertions
  need, and the resulting crash tells you less than a clean count does.

  | Path stripped | `test_geofence` | `test_camera` |
  | --- | --- | --- |
  | Assignment (`create_stream`, `update_stream`) | 6 fail | 4 fail |
  | Update (`update_geofence`, `update_drone`) | 7 fail — 36/43 | 7 fail — 34/41 |
  | Delete (`delete_geofence`, `delete_drone`) | 1 fail | 2 fail |

  Baseline is 43/43 and 41/41. **Every one of the ten predicates is guarded by at least
  one assertion that fails without it**, which is the property §9's history work
  established for the credential side and this section was written to demand.

  Two things the exercise turned up that reading the tests would not have.

  **Two of the three paths crash rather than failing cleanly.** When the delete filter
  goes, the other tenant's delete *succeeds* — so the row is genuinely gone and every
  later assertion that expects it raises instead of failing. The signal is loud, and it
  is honest about severity: cross-tenant destruction, not merely cross-tenant reads. But
  it truncates the run, so somebody testing only that case sees "1 FAIL" and does not
  learn the suite stopped. The suites are order-dependent on their own fixtures.

  **The failures reach further than ownership.** Removing an ownership filter also fails
  the *snapshot* assertions — "does NOT change the flight already recorded", "two
  tenants' flights carry two different fences" — because a tenant able to edit another's
  boundary also changes what that tenant's next flight is judged against. The two
  properties §11.2 and §11.3 describe separately are entangled in practice, which is an
  argument for both rather than a defect in either.

  **The DEM is not covered**, because §11.4's per-tenant raster is not built and there
  is no ownership path to break.
