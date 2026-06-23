# Cloud Architecture — Drone Monitoring Service

## 1. Executive Summary

The system provides real-time drone video analysis as a cloud service. A drone operator streams video and telemetry to a cloud endpoint; the service processes the stream and delivers a processed video feed plus structured alerts back to the operator's UI.

**The fundamental design constraint** is that video processing is real-time, latency-sensitive, and one-app-per-stream. This eliminates shared-processing architectures and drives the entire design toward **per-session isolated stacks deployed regionally close to the drone**.

---

## 2. Core Design Principles

| Principle | Consequence |
|---|---|
| One app handles one stream at a time | No shared app containers; each session gets a dedicated stack |
| Minimize video round-trip distance | Deploy the session stack in the cloud region nearest the drone operator |
| Stateless external persistence | Alerts go to a central DB outside the session stack; sessions can be torn down cleanly |
| Hard session boundaries | Each session is a fully isolated Docker Compose stack; a compromised or crashed session cannot affect others |
| TLS everywhere | No plaintext traffic on any external interface |

---

## 3. Deployment Model: Per-Session Isolated Stack

### Why not a shared monolith?

A single shared deployment would mean all users' video streams converge at one endpoint. Because video is high-bandwidth and latency-sensitive, a drone in Europe routing through a US cluster to reach a shared MediaMTX is unacceptable. Moreover, a shared app would require complex multiplexing with no throughput gain, since the GPU/CPU bottleneck is per-stream anyway.

### The per-session model

When a drone session is initiated, a **control plane** provisions a complete isolated stack in the cloud region nearest the operator. The stack runs for the duration of the session and is torn down when the session ends, freeing all resources.

```
Region EU-West                        Region AP-South
┌─────────────────────────┐           ┌─────────────────────────┐
│  Session Stack A        │           │  Session Stack B        │
│  (Drone operator in EU) │           │  (Drone operator in AP) │
└─────────────────────────┘           └─────────────────────────┘
         │                                       │
         └─────────────────┬─────────────────────┘
                           ▼
                  Central Control Plane
                  Central Database (global)
```

### Shared vs per-session component analysis

| Component | Decision | Rationale |
|---|---|---|
| App | **Per-session** | Hard constraint: one video at a time |
| MediaMTX | **Per-session** | Avoids cross-region video routing; simplifies auth |
| MQTT broker | **Per-session** | Avoids topic namespace collisions; simpler auth isolation |
| WebSocket server | **Per-session** | Tightly coupled to one session's alert stream |
| DB Worker | **Per-session** | Lightweight; no benefit from sharing |
| Reverse proxy | **Per-session** | Handles TLS for the session's public endpoint |
| Central DB | **Shared / external** | Persistent store across all sessions and regions |
| Control Plane | **Shared / global** | One API manages all regions |

---

## 4. Session Stack Components

### 4.1 Reverse Proxy — Traefik

**Role**: TLS termination, HTTP/WebSocket routing, authentication middleware.  
**Why Traefik over Nginx**: Traefik's dynamic configuration via Docker labels is ideal for programmatically-provisioned stacks. It integrates with Let's Encrypt for automatic TLS certificate provisioning per session domain.

Handles:
- HTTPS routing to MediaMTX (HLS/WebRTC/RTSP-over-HTTPS playback by viewers)
- WSS routing to the WebSocket server
- Optional: Bearer token validation middleware before forwarding to downstream services

RTSP publish from the drone (port 8554) and MQTT (port 1883/8883) are exposed directly as TCP/TLS — they bypass the HTTP proxy since they are not HTTP-based protocols.

### 4.2 MediaMTX

**Role**: Video ingestion from drone; re-publication of processed stream; playback to viewer UI.  
**Protocol surface**:
- Drone → MediaMTX: RTSP publish on a secret path (e.g. `rtsp://host:8554/session/{id}/raw?token=...`)
- App → MediaMTX: RTSP publish on processed path (e.g. `rtsp://mediamtx:8554/session/{id}/processed`)
- Viewer → MediaMTX: HLS or WebRTC (lower latency) via HTTPS, proxied through Traefik

**Configuration**: MediaMTX path-level authentication using per-session tokens provisioned by the control plane. External auth hook (HTTP callback) can validate tokens against the control plane.

### 4.3 MQTT Broker — Eclipse Mosquitto

**Role**: Receives telemetry (GPS, altitude, attitude, sensor data) from the drone; consumed by the app.  
**Protocol surface**:
- Drone → Mosquitto: MQTT over TLS (port 8883), authenticated with per-session credentials
- App ← Mosquitto: Internal subscription (no TLS needed on internal Docker network)

**Why Mosquitto over EMQX**: Mosquitto is minimal and has no overhead. EMQX is better for multi-tenant shared deployments; since this is per-session, Mosquitto is sufficient.

### 4.4 App

**Role**: Core processing pipeline. Consumes raw video from MediaMTX and telemetry from MQTT. Produces processed video (re-published to MediaMTX) and structured alerts (pushed to WebSocket server and DB worker).

**Interfaces** (all internal):
- MediaMTX raw stream → RTSP pull
- MQTT broker → subscribe to telemetry topics
- MediaMTX processed stream → RTSP publish
- WebSocket server → HTTP POST or internal message queue (e.g. Unix socket or Redis-free in-process channel)
- DB worker → HTTP POST or shared message queue

**Resource requirements**: GPU access required. The container must be scheduled on a GPU-equipped host. In Kubernetes this is a node selector; in a VM-based model the VM must have a GPU.

### 4.5 WebSocket Server

**Role**: Maintains a persistent WebSocket connection to the viewer's UI. Receives alert events from the app and pushes them to the connected client in real time.

**Protocol surface**:
- App → WebSocket server: Internal HTTP POST `/alert`
- Viewer ↔ WebSocket server: WSS, proxied through Traefik

**Implementation**: A minimal server (e.g. FastAPI + WebSockets, or a Node.js server). Stateless beyond the active connection — no DB, no queue. If the client disconnects and reconnects, alerts produced during the gap are retrieved from the DB (queried by the client on reconnect).

### 4.6 DB Worker

**Role**: Consumes alert events from the app and persists them to the external central database. Decouples the app from DB write latency.

**Protocol surface**:
- App → DB Worker: Internal HTTP POST `/ingest`
- DB Worker → External DB: Outbound TCP (PostgreSQL/other), authenticated

**Implementation**: A lightweight service that batches writes and retries on transient failures. Owns the schema migration logic. Does not expose any port to the external network.

---

## 5. Network Topology

```
                        INTERNET
                            │
           ┌────────────────┼────────────────┐
           │                │                │
    Drone (RTSP)     Drone (MQTT/TLS)    Viewer (HTTPS/WSS)
           │                │                │
           │                │                ▼
           │                │         ┌─────────────┐
           │                │         │   Traefik   │  ← TLS termination
           │                │         │  (proxy)    │    HTTPS → MediaMTX HLS/WebRTC
           │                │         │             │    WSS   → WebSocket Server
           │                │         └──────┬──────┘
           │                │                │
    ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
    │  MediaMTX   │  │  Mosquitto  │  │  WebSocket  │
    │  :8554(ext) │  │  :8883(ext) │  │  Server     │
    │  :8888(int) │  │  :1883(int) │  │  :8765(int) │
    └──────┬──────┘  └──────┬──────┘  └──────▲──────┘
           │  raw stream    │ telemetry        │ alerts
           └────────┬───────┘                 │
                    ▼                          │
             ┌─────────────┐                  │
             │     App     ├──────────────────┘
             │  (GPU host) │
             └──────┬──────┘
                    │ processed stream        alerts
                    ▼                          │
             ┌─────────────┐          ┌────────▼────────┐
             │  MediaMTX   │          │    DB Worker    │
             │  (same svc) │          │                 │
             └─────────────┘          └────────┬────────┘
                                               │
                                               ▼
                                      External Central DB
                                      (PostgreSQL / managed)
```

### Docker internal network

All containers are on a single internal bridge network (`session-net`). Only Traefik, MediaMTX (RTSP port), and Mosquitto (MQTT/TLS port) have external port bindings.

---

## 6. Docker Compose — Session Stack

The following is the canonical per-session `docker-compose.yml`. The control plane renders it with session-specific values at provisioning time (e.g. via `envsubst` or a template engine).

```yaml
# docker-compose.yml — per-session stack
# Rendered by control plane; SESSION_ID, SESSION_TOKEN_DRONE, SESSION_TOKEN_VIEWER,
# MQTT_USER, MQTT_PASS, ACME_EMAIL, SESSION_DOMAIN are injected at provision time.

name: session-${SESSION_ID}

networks:
  session-net:
    driver: bridge

volumes:
  mediamtx-config:
  mosquitto-config:
  mosquitto-data:
  letsencrypt:

services:

  traefik:
    image: traefik:v3.1
    restart: unless-stopped
    networks: [session-net]
    ports:
      - "80:80"
      - "443:443"
    command:
      - "--providers.docker=true"
      - "--providers.docker.network=session-net"
      - "--entrypoints.web.address=:80"
      - "--entrypoints.web.http.redirections.entrypoint.to=websecure"
      - "--entrypoints.websecure.address=:443"
      - "--certificatesresolvers.le.acme.tlschallenge=true"
      - "--certificatesresolvers.le.acme.email=${ACME_EMAIL}"
      - "--certificatesresolvers.le.acme.storage=/letsencrypt/acme.json"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - letsencrypt:/letsencrypt

  mediamtx:
    image: bluenviron/mediamtx:latest
    restart: unless-stopped
    networks: [session-net]
    ports:
      - "8554:8554"     # RTSP (drone publish, external)
      - "8322:8322"     # RTSPS — RTSP over TLS (optional)
    environment:
      MTX_LOGLEVEL: warn
      MTX_WEBRTCADDITIONALHOSTS: "${SESSION_DOMAIN}"
    volumes:
      - ./configs/mediamtx.yml:/mediamtx.yml:ro
    # MediaMTX config (mediamtx.yml) sets per-path publish tokens:
    #   paths:
    #     session/${SESSION_ID}/raw:
    #       source: publisher
    #       publishUser: drone
    #       publishPass: ${SESSION_TOKEN_DRONE}
    #     session/${SESSION_ID}/processed:
    #       source: publisher
    #       readUser: viewer
    #       readPass: ${SESSION_TOKEN_VIEWER}
    labels:
      - "traefik.enable=true"
      # HLS playback
      - "traefik.http.routers.mediamtx-hls.rule=Host(`${SESSION_DOMAIN}`) && PathPrefix(`/hls`)"
      - "traefik.http.routers.mediamtx-hls.entrypoints=websecure"
      - "traefik.http.routers.mediamtx-hls.tls.certresolver=le"
      - "traefik.http.routers.mediamtx-hls.service=mediamtx-svc"
      - "traefik.http.services.mediamtx-svc.loadbalancer.server.port=8888"
      # WebRTC signaling
      - "traefik.http.routers.mediamtx-webrtc.rule=Host(`${SESSION_DOMAIN}`) && PathPrefix(`/webrtc`)"
      - "traefik.http.routers.mediamtx-webrtc.entrypoints=websecure"
      - "traefik.http.routers.mediamtx-webrtc.tls.certresolver=le"
      - "traefik.http.routers.mediamtx-webrtc.service=mediamtx-svc"

  mosquitto:
    image: eclipse-mosquitto:2
    restart: unless-stopped
    networks: [session-net]
    ports:
      - "8883:8883"     # MQTT over TLS (drone publish, external)
    volumes:
      - ./configs/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
      - ./configs/mosquitto-passwd:/mosquitto/config/passwd:ro
      - ./certs/server.crt:/mosquitto/certs/server.crt:ro
      - ./certs/server.key:/mosquitto/certs/server.key:ro
      - mosquitto-data:/mosquitto/data
    # mosquitto.conf sets TLS listener on 8883 and plaintext on 1883 (internal only)
    # passwd file generated by control plane: mosquitto_passwd -c passwd ${MQTT_USER} ${MQTT_PASS}

  app:
    image: ${APP_IMAGE}
    restart: unless-stopped
    networks: [session-net]
    runtime: nvidia     # GPU access; requires nvidia-container-toolkit on host
    environment:
      SESSION_ID: "${SESSION_ID}"
      MEDIAMTX_URL: "rtsp://mediamtx:8554"
      RAW_STREAM_PATH: "session/${SESSION_ID}/raw"
      PROCESSED_STREAM_PATH: "session/${SESSION_ID}/processed"
      MEDIAMTX_PUBLISH_USER: "app"
      MEDIAMTX_PUBLISH_PASS: "${SESSION_TOKEN_APP}"
      MQTT_HOST: "mosquitto"
      MQTT_PORT: "1883"
      MQTT_USER: "${MQTT_USER}"
      MQTT_PASS: "${MQTT_PASS}"
      WS_SERVER_URL: "http://ws-server:8765"
      DB_WORKER_URL: "http://db-worker:9000"
    depends_on:
      - mediamtx
      - mosquitto
      - ws-server
      - db-worker

  ws-server:
    image: ${WS_SERVER_IMAGE}
    restart: unless-stopped
    networks: [session-net]
    environment:
      SESSION_ID: "${SESSION_ID}"
      VIEWER_TOKEN: "${SESSION_TOKEN_VIEWER}"
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.ws.rule=Host(`${SESSION_DOMAIN}`) && PathPrefix(`/ws`)"
      - "traefik.http.routers.ws.entrypoints=websecure"
      - "traefik.http.routers.ws.tls.certresolver=le"
      - "traefik.http.services.ws-svc.loadbalancer.server.port=8765"

  db-worker:
    image: ${DB_WORKER_IMAGE}
    restart: unless-stopped
    networks: [session-net]
    environment:
      DATABASE_URL: "${DATABASE_URL}"    # injected from control plane secrets
      SESSION_ID: "${SESSION_ID}"
    # No external ports — outbound only to external DB
```

---

## 7. Control Plane

The control plane is a separate long-running service (single global deployment, or replicated with a load balancer). It is not part of the per-session stack.

### Responsibilities

1. **Session lifecycle management**: Create, monitor, and destroy session stacks
2. **Credential generation**: Issue per-session tokens for drone, viewer, app, MQTT
3. **Regional dispatch**: Select the nearest cloud region based on operator location or explicit preference
4. **DNS provisioning**: Register `{session-id}.{region}.yourdomain.com` → session host IP
5. **Health monitoring**: Poll session containers; alert on failures; auto-teardown on disconnect
6. **User/billing management**: Associate sessions with user accounts

### Provisioning flow

```
Client (mobile app / web UI)
   │
   POST /sessions  { region: "eu-west", drone_id: "..." }
   │
Control Plane API
   ├── Generate SESSION_ID, tokens, credentials
   ├── Select target VM in eu-west (or provision new one)
   ├── SSH/API: render docker-compose.yml from template
   ├── SSH/API: docker compose up -d
   ├── Register DNS: {SESSION_ID}.eu-west.yourdomain.com → VM IP
   └── Return to client:
         {
           rtsp_url:  "rtsp://{SESSION_ID}.eu-west.yourdomain.com:8554/session/{id}/raw",
           mqtt_url:  "mqtts://{SESSION_ID}.eu-west.yourdomain.com:8883",
           hls_url:   "https://{SESSION_ID}.eu-west.yourdomain.com/hls/session/{id}/processed",
           ws_url:    "wss://{SESSION_ID}.eu-west.yourdomain.com/ws",
           drone_token: "...",
           viewer_token: "..."
         }
```

### Teardown trigger

- Drone disconnects from MediaMTX (MediaMTX webhook → control plane)
- Session timeout (configurable, e.g. 30 min after last activity)
- Explicit `DELETE /sessions/{id}` from client

On teardown: `docker compose down -v` → deregister DNS → release VM (or return to pool).

---

## 8. Regional Edge Deployment

### VM pool strategy

Maintain a warm pool of GPU-equipped VMs in each supported region. A warm VM has Docker and the nvidia-container-toolkit pre-installed and is ready to receive a session stack immediately.

| Approach | Latency to start | Cost |
|---|---|---|
| Warm pool (1-2 VMs per region) | < 10 s | Pay for idle VMs |
| On-demand VM provisioning | 60–120 s | No idle cost |
| Kubernetes node auto-scaling | 30–90 s | Complex but elastic |

**Recommendation**: Warm pool with 1–2 VMs per active region. Use a simple VM size that fits one session (GPU + 16–32 GB RAM). If sessions rarely overlap per region, this is the lowest-complexity option. Add a second VM if concurrent sessions are needed.

### Supported regions (example)

| Region label | Cloud provider location | Covers |
|---|---|---|
| `eu-west` | AWS eu-west-1 / GCP europe-west1 | Europe |
| `us-east` | AWS us-east-1 / GCP us-east4 | Americas |
| `ap-south` | AWS ap-southeast-1 / GCP asia-southeast1 | Asia-Pacific |

The control plane selects region based on the operator's declared location or IP geolocation at session creation.

---

## 9. Security Model

### External trust boundaries

```
Drone        → MediaMTX (RTSP/TLS): per-session publish token, rotated each session
Drone        → Mosquitto (MQTT/TLS): per-session username+password, CA-signed cert on server
Viewer       → Traefik (HTTPS/WSS): Bearer token (JWT) validated by Traefik middleware or by ws-server
Control Plane→ VM: SSH with provisioning key, or cloud provider instance API
DB Worker    → External DB: connection string with session-scoped DB user (read/write to session's partition only)
```

### Internal network

All inter-container communication is on the isolated `session-net` bridge. No container has host networking. Internal ports (1883, 8765, 8888, 9000) are not bound to the host.

### Secrets

- All session tokens are generated by the control plane using a CSPRNG (e.g. `secrets.token_urlsafe(32)`)
- Tokens are injected as environment variables at compose render time
- Tokens are stored in the control plane DB encrypted at rest
- No secrets are baked into images

### Certificate strategy

- **MediaMTX RTSP (port 8554)**: Use Mosquitto-style pre-provisioned TLS cert (wildcard cert for `*.region.yourdomain.com`, renewed by control plane, copied to VM before session start)
- **HTTP/WSS services**: Traefik handles Let's Encrypt ACME automatically per session domain
- **MQTT (port 8883)**: Mosquitto TLS using the same wildcard cert

---

## 10. Central Database

The external DB is not part of any session stack. It is a managed PostgreSQL instance (e.g. AWS RDS, Supabase, Neon) in a central region.

### Schema sketch

```sql
sessions (
  id          UUID PRIMARY KEY,
  user_id     UUID REFERENCES users(id),
  region      TEXT,
  started_at  TIMESTAMPTZ,
  ended_at    TIMESTAMPTZ,
  drone_id    TEXT
)

alerts (
  id          UUID PRIMARY KEY,
  session_id  UUID REFERENCES sessions(id),
  created_at  TIMESTAMPTZ,
  alert_type  TEXT,
  severity    TEXT,
  payload     JSONB,
  frame_ts    FLOAT8   -- timestamp in the video stream
)
```

The DB worker for each session writes to `alerts` with its `SESSION_ID`. On reconnect, the viewer client queries `GET /sessions/{id}/alerts?since=...` from the control plane API, which proxies to the DB.

---

## 11. Data Flow Summary

```
DRONE
  │─── RTSP publish ──────────────────► MediaMTX [:8554]
  │─── MQTT publish ──────────────────► Mosquitto [:8883]
                                              │              │
                                              ▼              ▼
                                           App ◄────────────┘
                                            │
                     ┌──────────────────────┼──────────────────────┐
                     ▼                      ▼                      ▼
               RTSP publish           HTTP POST /alert       HTTP POST /ingest
               (processed)            to ws-server           to db-worker
                     │                      │                      │
                     ▼                      ▼                      ▼
               MediaMTX            WebSocket push          Write to external DB
               [:8554/processed]   to viewer UI
                     │                      │
                     └──────────┬───────────┘
                                ▼
                             Traefik
                             HTTPS/WSS
                                │
                             VIEWER UI
                       (HLS/WebRTC video + WSS alerts)
```

---

## 12. Production Readiness Checklist

- [ ] GPU driver + nvidia-container-toolkit pre-installed on all session VMs
- [ ] Wildcard TLS certificate provisioned and auto-renewed (wildcard covers RTSP/MQTT ports; Traefik covers HTTP)
- [ ] MediaMTX auth hook configured (validates drone token against control plane)
- [ ] Mosquitto password files generated per-session (not shared config)
- [ ] DB Worker retry logic with exponential backoff; dead-letter queue for failed writes
- [ ] Control plane health probe: poll MediaMTX `/v3/paths/list`; auto-teardown on stream absence > threshold
- [ ] Session maximum duration enforced (prevents zombie sessions)
- [ ] Resource limits set on all containers (`deploy.resources.limits` in compose) — prevent a runaway app from starving the broker
- [ ] Log aggregation: forward container logs to a central collector (e.g. Loki, CloudWatch) with `session_id` label
- [ ] Metrics: expose `/metrics` from app, ws-server, db-worker; scrape with Prometheus on the VM; push to central Grafana
- [ ] VM image baked with Docker, nvidia-toolkit, compose plugin — cold-start time < 30 s
- [ ] Firewall rules: only ports 80, 443, 8554, 8883 open externally; block all others at cloud security-group level
- [ ] External DB has automated backups, connection pooling (PgBouncer), and a read replica if alert queries are heavy

---

## 13. Alternative: Kubernetes (when to graduate)

The Docker Compose + VM pool model is the right starting point. Graduate to Kubernetes when:

- **Concurrent sessions per region exceed ~5–10**: K8s bin-packing optimizes GPU utilization across nodes
- **Multi-GPU workloads**: K8s GPU fractional sharing (MIG, Time-slicing)
- **Session startup SLA < 5 s**: K8s pre-warmed pod pools (VPA + cluster autoscaler)

In K8s, each session becomes a **Namespace** with its own deployments for each component. Traefik or Nginx Ingress handles routing at the cluster level. The control plane becomes a Kubernetes Operator.

---

*Document version: 1.0 — designed for single-stream, latency-sensitive drone monitoring. Revise section 8 (regional capacity) based on observed concurrent user growth.*
