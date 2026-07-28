# Cloud Architecture — Drone Monitoring Service

## 1. Executive Summary

The system provides real-time drone video analysis as a cloud service. A drone operator streams video and telemetry to a cloud endpoint; the service processes the stream and delivers a processed video feed plus structured alerts back to the operator's UI. Completed recording segments are automatically uploaded to configurable cloud storage.

**The fundamental design constraint** is that video processing is real-time, latency-sensitive, and one-app-per-stream. This eliminates shared-processing architectures and drives the entire design toward **per-session isolated stacks deployed regionally close to the drone**.

---

## 2. Core Design Principles

| Principle | Consequence |
| --------- | ----------- |
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

```text
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
| --------- | -------- | --------- |
| App | **Per-session** | Hard constraint: one video at a time |
| MediaMTX | **Per-session** | Avoids cross-region video routing; simplifies auth |
| MQTT broker | **Per-session** | Avoids topic namespace collisions; simpler auth isolation |
| WebSocket server | **Per-session** | Tightly coupled to one session's alert stream |
| DB Worker | **Per-session** | Lightweight; no benefit from sharing |
| Recorder | **Per-session** | Uploads segments produced by this session's MediaMTX only |
| Reverse proxy | **Per-session** | Handles TLS for the session's public endpoint |
| Central DB | **Shared / external** | Persistent store across all sessions and regions |
| Control Plane | **Shared / global** | One API manages all regions |

---

## 4. Session Stack Components

### 4.1 Reverse Proxy — Traefik

**Role**: TLS termination, HTTP/WebSocket routing, authentication middleware.  
**Why Traefik over Nginx**: Traefik's dynamic configuration via Docker labels is ideal for programmatically-provisioned stacks. It integrates with Let's Encrypt for automatic TLS certificate provisioning per session domain.

Handles:

- HTTPS routing to MediaMTX (HLS/WebRTC playback by viewers)
- WSS routing to the WebSocket server
- Optional: Bearer token validation middleware before forwarding to downstream services

RTSP publish from the drone (port 8554) and MQTT (port 1883/8883) are exposed directly as TCP/TLS — they bypass the HTTP proxy since they are not HTTP-based protocols.

### 4.2 MediaMTX

**Role**: Video ingestion from drone; re-publication of processed stream; playback to viewer UI; **recording of the annotated stream**.  
**Protocol surface**:

- Drone → MediaMTX: RTSP publish on a secret path (e.g. `rtsp://host:8554/session/{id}/raw?token=...`)
- App → MediaMTX: RTMP publish on annotated path (`rtmp://mediamtx:1935/annot`)
- Viewer → MediaMTX: HLS or WebRTC (lower latency) via HTTPS, proxied through Traefik
- MediaMTX → Recorder: `runOnRecordSegmentComplete` webhook (`wget` POST to `http://recorder:8000/on-segment-complete`) on each completed segment

**Recording**: MediaMTX records the `annot` path to the shared `recordings` volume in fmp4 format. This removes the need for the app to write video files locally and eliminates double-encoding.

**Configuration**: MediaMTX path-level authentication using per-session tokens provisioned by the control plane. External auth hook (HTTP callback) can validate tokens against the control plane.

### 4.3 MQTT Broker — Eclipse Mosquitto

**Role**: Receives telemetry (GPS, altitude, attitude, sensor data) from the drone; consumed by the app.  
**Protocol surface**:

- Drone → Mosquitto: MQTT over TLS (port 8883), authenticated with per-session credentials
- App ← Mosquitto: Internal subscription (no TLS needed on internal Docker network)

**Why Mosquitto over EMQX**: Mosquitto is minimal and has no overhead. EMQX is better for multi-tenant shared deployments; since this is per-session, Mosquitto is sufficient.

### 4.4 App

**Role**: Core processing pipeline. Consumes raw video from MediaMTX and telemetry from MQTT. Produces annotated video (pushed to MediaMTX via RTMP) and structured alerts (pushed to WebSocket server and DB worker).

**Interfaces** (all internal):

- MediaMTX raw stream → RTSP pull
- MQTT broker → subscribe to telemetry topics
- MediaMTX annotated stream → RTMP push
- WebSocket server → HTTP POST to `http://ws-server:8000/alert`
- DB worker → HTTP POST to `http://db-writer:8000/save_alert`

**Resource requirements**: GPU access required. The container must be scheduled on a GPU-equipped host. Requires `shm_size: 256m` for POSIX shared memory frame buffers. In Kubernetes this is a node selector; in a VM-based model the VM must have a GPU.

### 4.5 WebSocket Server

**Role**: Maintains a persistent WebSocket connection to the viewer's UI. Receives alert events from the app and pushes them to the connected client in real time.

**Protocol surface**:

- App → WebSocket server: Internal HTTP POST to `/alert` on port 8000 (FastAPI HTTP API)
- Viewer ↔ WebSocket server: WSS on the configurable `WS_PORT` (default 8765), proxied through Traefik
- External: `wss://<domain>/ws` via Traefik, or `ws://host:8765` direct

**Implementation**: FastAPI + WebSockets. The WebSocket listener runs in a background thread; the HTTP API runs in the uvicorn event loop. Stateless beyond the active connection — no DB, no queue. If the client disconnects and reconnects, alerts produced during the gap are retrieved from the DB (queried by the client on reconnect). Shutdown is triggered by uvicorn receiving SIGTERM (as PID 1 in the container), which resumes the FastAPI lifespan context and calls `WebSocketManager.stop()`.

### 4.6 DB Worker

**Role**: Consumes alert events from the app and persists them to the external central database. Decouples the app from DB write latency.

**Protocol surface**:

- App → DB Worker: Internal HTTP POST to `/save_alert` on port 8000
- DB Worker → External DB: Outbound TCP (PostgreSQL), authenticated

**Implementation**: A lightweight FastAPI service with an internal async queue and background worker thread. Owns the schema migration logic. Returns HTTP 503 if the alert queue is full (DB unreachable for extended period). Does not expose any port to the external network.

### 4.7 Recorder

**Role**: Receives a webhook from MediaMTX on each completed recording segment and uploads the file to the configured storage backend (local volume, Azure Blob Storage, or AWS S3).

**Protocol surface**:

- MediaMTX → Recorder: Internal HTTP POST to `/on-segment-complete` (form data: `path=<segment-file-path>`)
- Recorder → Azure / S3: Outbound HTTPS upload

**Implementation**: A minimal FastAPI service. The endpoint returns immediately (`202 Accepted`) and performs the upload in a FastAPI `BackgroundTask`, so the MediaMTX webhook does not block. Upload failures are logged and skipped — the segment file is retained on the `recordings` volume in all cases. The storage SDK (azure-storage-blob / boto3) is imported lazily at first use. Configured entirely via environment variables (`RECORDING_STORE_SERVICE`, `RECORDING_AZURE_*`, `RECORDING_AWS_*`).

---

## 5. Network Topology

```text
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
    │  :8888(int) │  │  :1883(int) │  │  :8000(api) │
    └──────┬──────┘  └──────┬──────┘  │  :8765(ws)  │
           │  raw stream    │ telemetry└──────▲──────┘
           └────────┬───────┘                 │ alerts
                    ▼                         │
             ┌─────────────┐                  │
             │     App     ├──────────────────┘
             │  (GPU host) │
             └──────┬──────┘
           annotated│stream          alerts
                    ▼                 │
             ┌─────────────┐  ┌───────▼─────────┐
             │  MediaMTX   │  │    DB Worker    │
             │  (same svc) │  │    :8000(api)   │
             │  records to │  └────────┬────────┘
             │  /recordings│           │
             └──────┬──────┘           ▼
                    │         External Central DB
                    │         (PostgreSQL / managed)
                    ▼
             ┌─────────────┐
             │  Recorder   │  ← webhook from MediaMTX
             │  :8000      │
             └──────┬──────┘
                    │ upload
                    ▼
          Azure Blob / S3 / local
```

### Docker internal network

All containers are on a single internal bridge network (`session-net`). Only Traefik (80, 443), MediaMTX (8554, 1935, 8889), Mosquitto (1883), and the WebSocket server (`WS_PORT`) have external port bindings. The recorder, db-worker, ws-server API, and postgres are internal-only.

---

## 6. Docker Compose — Session Stack

The following is the canonical per-session `docker-compose.yml`. The control plane renders it with session-specific values at provisioning time (e.g. via `envsubst` or a template engine).

```yaml
# docker-compose.yml — per-session stack
# Rendered by control plane; SESSION_ID, ACME_EMAIL, SESSION_DOMAIN,
# MQTT credentials, and storage secrets are injected at provision time.

name: session-${SESSION_ID}

networks:
  session-net:
    driver: bridge

volumes:
  mosquitto-data:
  postgres-data:
  letsencrypt:
  recordings:

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
      - "--providers.docker.exposedbydefault=false"
      - "--entrypoints.web.address=:80"
      - "--entrypoints.websecure.address=:443"
      - "--certificatesresolvers.letsencrypt.acme.httpchallenge=true"
      - "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web"
      - "--certificatesresolvers.letsencrypt.acme.email=${ACME_EMAIL}"
      - "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - letsencrypt:/letsencrypt

  mediamtx:
    image: bluenviron/mediamtx:latest
    restart: unless-stopped
    networks: [session-net]
    depends_on:
      recorder:
        condition: service_healthy
    ports:
      - "8554:8554"     # RTSP  — drone publishes here
      - "1935:1935"     # RTMP  — app pushes annotated video here
      - "8889:8889"     # WebRTC — viewer connects here
    volumes:
      - ./configs/mediamtx/mediamtx.yaml:/mediamtx.yml:ro
      - recordings:/recordings
    # mediamtx.yaml configures the annot path with:
    #   record: yes
    #   recordPath: /recordings/%path/%Y-%m-%d_%H-%M-%S-%f
    #   recordFormat: fmp4
    #   runOnRecordSegmentComplete: wget -q -O /dev/null \
    #     --post-data="path=$MTX_SEGMENT_PATH" http://recorder:8000/on-segment-complete
    labels:
      - "traefik.enable=true"
      - "traefik.http.middlewares.hls-strip.stripprefix.prefixes=/hls"
      - "traefik.http.routers.hls.rule=PathPrefix(`/hls`)"
      - "traefik.http.routers.hls.entrypoints=websecure"
      - "traefik.http.routers.hls.tls.certresolver=letsencrypt"
      - "traefik.http.routers.hls.middlewares=hls-strip"
      - "traefik.http.routers.hls.service=hls-svc"
      - "traefik.http.services.hls-svc.loadbalancer.server.port=8888"
      - "traefik.http.middlewares.webrtc-strip.stripprefix.prefixes=/webrtc"
      - "traefik.http.routers.webrtc.rule=PathPrefix(`/webrtc`)"
      - "traefik.http.routers.webrtc.entrypoints=websecure"
      - "traefik.http.routers.webrtc.tls.certresolver=letsencrypt"
      - "traefik.http.routers.webrtc.middlewares=webrtc-strip"
      - "traefik.http.routers.webrtc.service=webrtc-svc"
      - "traefik.http.services.webrtc-svc.loadbalancer.server.port=8889"

  mosquitto:
    image: eclipse-mosquitto:2
    restart: unless-stopped
    networks: [session-net]
    ports:
      - "1883:1883"
    volumes:
      - ./configs/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
      - mosquitto-data:/mosquitto/data

  postgres:
    image: postgres:16
    restart: unless-stopped
    networks: [session-net]
    environment:
      POSTGRES_DB:       ${POSTGRES_DB:-agrarian_db}
      POSTGRES_USER:     ${POSTGRES_USER:-db_user}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-db_pass}
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-db_user}"]
      interval: 5s
      timeout: 5s
      retries: 12

  db-writer:
    image: ${DB_WRITER_IMAGE}
    restart: unless-stopped
    networks: [session-net]
    environment:
      DB_SERVICE:         postgresql
      DB_HOST:            postgres
      DB_NAME:            ${POSTGRES_DB:-agrarian_db}
      DB_WORKER_NAME:     ${POSTGRES_USER:-db_user}
      DB_WORKER_PASSWORD: ${POSTGRES_PASSWORD:-db_pass}
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\""]
      interval: 5s
      timeout: 5s
      retries: 12

  ws-server:
    image: ${WS_SERVER_IMAGE}
    restart: unless-stopped
    networks: [session-net]
    ports:
      - "${WS_PORT:-8765}:${WS_PORT:-8765}"
    environment:
      WS_PORT: ${WS_PORT:-8765}
    labels:
      - "traefik.enable=true"
      - "traefik.http.services.ws-svc.loadbalancer.server.port=${WS_PORT:-8765}"
      - "traefik.http.routers.wss.rule=PathPrefix(`/ws`)"
      - "traefik.http.routers.wss.entrypoints=websecure"
      - "traefik.http.routers.wss.tls.certresolver=letsencrypt"
      - "traefik.http.routers.wss.service=ws-svc"
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\""]
      interval: 5s
      timeout: 5s
      retries: 12

  recorder:
    image: ${RECORDER_IMAGE}
    restart: unless-stopped
    networks: [session-net]
    environment:
      RECORDING_STORE_SERVICE:           ${RECORDING_STORE_SERVICE:-local}
      RECORDING_DELETE_LOCAL_ON_SUCCESS: ${RECORDING_DELETE_LOCAL_ON_SUCCESS:-false}
      RECORDING_AZURE_CONNECTION_STRING: ${RECORDING_AZURE_CONNECTION_STRING:-}
      RECORDING_AZURE_CONTAINER_NAME:    ${RECORDING_AZURE_CONTAINER_NAME:-}
      RECORDING_AZURE_BLOB_PREFIX:       ${RECORDING_AZURE_BLOB_PREFIX:-}
      RECORDING_AWS_BUCKET_NAME:         ${RECORDING_AWS_BUCKET_NAME:-}
      RECORDING_AWS_KEY_PREFIX:          ${RECORDING_AWS_KEY_PREFIX:-}
      RECORDING_AWS_ACCESS_KEY_ID:       ${RECORDING_AWS_ACCESS_KEY_ID:-}
      RECORDING_AWS_SECRET_ACCESS_KEY:   ${RECORDING_AWS_SECRET_ACCESS_KEY:-}
      RECORDING_AWS_REGION_NAME:         ${RECORDING_AWS_REGION_NAME:-}
    volumes:
      - recordings:/recordings
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\""]
      interval: 5s
      timeout: 5s
      retries: 12

  app:
    image: ${APP_IMAGE}
    restart: unless-stopped
    networks: [session-net]
    runtime: nvidia
    shm_size: "256m"
    env_file: app/.env
    environment:
      WS_SERVER_URL: http://ws-server:8000   # internal wiring — not user-configurable
      DB_WRITER_URL: http://db-writer:8000   # internal wiring — not user-configurable
    depends_on:
      db-writer:
        condition: service_healthy
      ws-server:
        condition: service_healthy
      mediamtx:
        condition: service_started
      mosquitto:
        condition: service_started
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

```text
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
           rtsp_url:     "rtsp://{SESSION_ID}.eu-west.yourdomain.com:8554/...",
           mqtt_url:     "mqtts://{SESSION_ID}.eu-west.yourdomain.com:8883",
           hls_url:      "https://{SESSION_ID}.eu-west.yourdomain.com/hls/annot/index.m3u8",
           webrtc_url:   "https://{SESSION_ID}.eu-west.yourdomain.com/webrtc/annot/whep",
           ws_url:       "wss://{SESSION_ID}.eu-west.yourdomain.com/ws",
           drone_token:  "...",
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
| -------- | ---------------- | ---- |
| Warm pool (1-2 VMs per region) | < 10 s | Pay for idle VMs |
| On-demand VM provisioning | 60–120 s | No idle cost |
| Kubernetes node auto-scaling | 30–90 s | Complex but elastic |

**Recommendation**: Warm pool with 1–2 VMs per active region. Use a simple VM size that fits one session (GPU + 16–32 GB RAM). If sessions rarely overlap per region, this is the lowest-complexity option. Add a second VM if concurrent sessions are needed.

### Supported regions (example)

| Region label | Cloud provider location | Covers |
| ------------ | ----------------------- | ------ |
| `eu-west` | AWS eu-west-1 / GCP europe-west1 | Europe |
| `us-east` | AWS us-east-1 / GCP us-east4 | Americas |
| `ap-south` | AWS ap-southeast-1 / GCP asia-southeast1 | Asia-Pacific |

The control plane selects region based on the operator's declared location or IP geolocation at session creation.

---

## 9. Security Model

### External trust boundaries

```text
Drone        → MediaMTX (RTSP/TLS): per-session publish token, rotated each session
Drone        → Mosquitto (MQTT/TLS): per-session username+password, CA-signed cert on server
Viewer       → Traefik (HTTPS/WSS): Bearer token (JWT) validated by Traefik middleware or by ws-server
Control Plane→ VM: SSH with provisioning key, or cloud provider instance API
DB Worker    → External DB: connection string with session-scoped DB user (read/write to session's partition only)
Recorder     → Azure / S3: storage credentials injected at provision time; scoped to the session's blob prefix / key prefix
```

### Internal network

All inter-container communication is on the isolated `session-net` bridge. No container has host networking. The recorder, db-writer, ws-server HTTP API, and postgres ports are not bound to the host.

### Secrets

- All session tokens are generated by the control plane using a CSPRNG (e.g. `secrets.token_urlsafe(32)`)
- Tokens are injected as environment variables at compose render time
- Tokens are stored in the control plane DB encrypted at rest
- No secrets are baked into images

### Certificate strategy

- **HTTP/WSS services**: Traefik handles Let's Encrypt ACME automatically per session domain (HTTP challenge on port 80)
- **MQTT (port 8883)**: Mosquitto TLS using a wildcard cert for `*.region.yourdomain.com`, provisioned by the control plane
- **RTSP (port 8554)**: Optionally wrapped in TLS using the same wildcard cert

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

```text
DRONE
  │─── RTSP publish ──────────────────► MediaMTX [:8554]
  │─── MQTT publish ──────────────────► Mosquitto [:8883]
                                              │              │
                                              ▼              ▼
                                           App ◄────────────┘
                                            │
                     ┌──────────────────────┼──────────────────────┐
                     ▼                      ▼                      ▼
               RTMP publish           HTTP POST /alert       HTTP POST /save_alert
               (annotated)            to ws-server           to db-worker
                     │                      │                      │
                     ▼                      ▼                      ▼
               MediaMTX            WebSocket push          Write to external DB
               [:1935/annot]       to viewer UI
                     │                      │
                     │    ┌─────────────────┘
                     │    ▼
                     │  Traefik
                     │  HTTPS/WSS
                     │    │
                     │  VIEWER UI
                     │  (HLS/WebRTC video + WSS alerts)
                     │
                     ▼  (record to /recordings volume)
               Recorder ◄── webhook on segment complete
                     │
                     ▼
          Azure Blob / S3 / local volume
```

---

## 12. Production Readiness Checklist

- [ ] GPU driver + nvidia-container-toolkit pre-installed on all session VMs
- [ ] `shm_size: 256m` set on the app container (POSIX SHM frame buffers require it)
- [ ] Wildcard TLS certificate provisioned and auto-renewed (wildcard covers RTSP/MQTT ports; Traefik covers HTTP/WSS)
- [ ] MediaMTX auth hook configured (validates drone token against control plane)
- [ ] Mosquitto password files generated per-session (not shared config)
- [ ] DB Worker queue depth monitored; `503` responses from `/save_alert` surface in app logs as warnings
- [ ] Recorder upload failures logged and monitored; recordings volume sized for worst-case retention before upload
- [ ] `RECORDING_DELETE_LOCAL_ON_SUCCESS=true` set when using cloud storage, to prevent the `recordings` volume from filling
- [ ] Storage credentials for recorder scoped to session's prefix (least-privilege)
- [ ] Control plane health probe: poll MediaMTX `/v3/paths/list`; auto-teardown on stream absence > threshold
- [ ] Session maximum duration enforced (prevents zombie sessions)
- [ ] Resource limits set on all containers (`deploy.resources.limits` in compose) — prevent a runaway app from starving the broker
- [ ] Log aggregation: forward container logs to a central collector (e.g. Loki, CloudWatch) with `session_id` label
- [ ] Metrics: expose `/metrics` from app, ws-server, db-writer, recorder; scrape with Prometheus; push to central Grafana
- [ ] VM image baked with Docker, nvidia-toolkit, compose plugin — cold-start time < 30 s
- [ ] Firewall rules: only ports 80, 443, 8554, 1935, 8889, 1883 open externally; block all others at cloud security-group level
- [ ] External DB has automated backups, connection pooling (PgBouncer), and a read replica if alert queries are heavy

---

## 13. Alternative: Kubernetes (when to graduate)

The Docker Compose + VM pool model is the right starting point. Graduate to Kubernetes when:

- **Concurrent sessions per region exceed ~5–10**: K8s bin-packing optimizes GPU utilization across nodes
- **Multi-GPU workloads**: K8s GPU fractional sharing (MIG, Time-slicing)
- **Session startup SLA < 5 s**: K8s pre-warmed pod pools (VPA + cluster autoscaler)

In K8s, each session becomes a **Namespace** with its own deployments for each component. Traefik or Nginx Ingress handles routing at the cluster level. The control plane becomes a Kubernetes Operator.

---

*Document version: 1.1 — added recorder sidecar (MediaMTX-based recording replacing in-app video persistence); updated port references; revised data flow and security model accordingly.*
