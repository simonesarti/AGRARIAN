# AGRARIAN — PILOT 1

![AGRARIAN](assets/agrarian.png)

Drone-based livestock monitoring pipeline with two operating modes, selected at runtime via the `APP_MODE` environment variable:

- **`danger_detection`** — detects animals and obstacles in the flight path, uses a DEM for terrain-aware safety assessment
- **`health_monitoring`** — tracks animals over time and flags behavioural anomalies

---

## Pipeline Architecture

Both pipelines are chains of `multiprocessing.Process` workers connected by shared-memory frame buffers. All IPC passes through two buffer classes defined in [app/shared/processes/frame_buffer.py](app/shared/processes/frame_buffer.py):

- **`FrameBuffer`** — a pool of N POSIX shared memory slots, each holding one `(H, W, 3)` BGR frame. Workers call `acquire()` → `write()` → enqueue metadata; the downstream worker calls `view()` (zero-copy read into SHM) → process → `release()`.
- **`MultiFrameBuffer`** — two independent POSIX SHM regions per slot (primary for the frame, secondary for a mask stack), governed by a single slot pool. The primary region is `(H, W, 3)` HWC (cv2-native, always contiguous); the secondary is `(N, H, W)` CHW so that each mask `mask_view[i]` is a contiguous `(H, W)` slice. This layout eliminates intermediate `np.concatenate` allocations and `np.ascontiguousarray` copies.

Each hop has its own N-slot pool. When a downstream consumer is too slow to free slots, the producer drops the current frame at that hop — slow stages never stall faster ones.

### Danger Detection

```text
      Video Reader
           │
          FB
           │
        Combiner  ◄── MQTT telemetry
           │
          FB
           │
        Detection
           │
          FB
           │
      Segmentation
           │
  MFB [roads, vehicles]
           │
          Geo
           │
  MFB [roads, vehicles, nodata, geofencing, slope]
           │
     Danger Worker
           │
  MFB [danger, intersection]
           │
   Annotation Worker
         ┌─┴─┐
         │   │
        FB   FB
         │   │
      Alert  Video
      Writer Producer ──► RTMP → MediaMTX
```

`FB` = `FrameBuffer((H,W,3))`, `MFB` = `MultiFrameBuffer`

Segmentation and geo inference use TensorRT. Both the YOLO detection process and the TensorRT process run on the GPU concurrently via NVIDIA MPS — start `nvidia-cuda-mps-control` on the host before launching the container.

### Health Monitoring

```text
     Video Reader
          │
         FB
          │
       Tracking
          │
         FB
          │
  Anomaly Detector
          │
         FB
          │
     Interpolator
          │
         FB
          │
  Annotation Worker
        ┌─┴─┐
        │   │
       FB   FB
        │   │
     Alert  Video
     Writer Producer ──► RTMP → MediaMTX
```

In engine mode (TensorRT `.engine` file present) every frame is tracked; in fallback mode (`.pt` checkpoint) 1 in 4 frames is tracked to compensate for higher inference latency. Health monitoring does not require NVIDIA MPS.

---

## Service Stack

The stack is split across two independent deployments:

- [docker-compose.yml](docker-compose.yml) — communication services, managed by Docker Compose (partner VM or local)
- **app** — a single standalone container run with `docker run` (GPU machine)

| Service | Deployment | Role |
| ------- | ---------- | ---- |
| **mediamtx** | comms | Video ingestion from drone (RTSP); re-publishes annotated stream (RTMP); records the annotated stream to the `recordings` volume |
| **mosquitto** | comms | MQTT broker; receives drone telemetry consumed by the app |
| **db-writer** | comms | Receives alert POST requests from the app and writes them to the partner-hosted database; decouples the app from DB write latency |
| **ws-server** | comms | Maintains a WebSocket connection to the viewer UI; receives alert events from the app and pushes them in real time |
| **recorder** | comms | Receives a webhook from MediaMTX on each completed recording segment and uploads it to the configured storage backend |
| **orchestrator** | comms | Starts one app container per flight when a stream goes live and stops it when the publisher drops |
| **portal** | comms | The account pages — sign-up, sign-in, stream slots and the live view. The only service on the public side that talks to db-writer |
| **app** | standalone | Core GPU processing pipeline; consumes video and telemetry, produces annotated stream and structured alerts |

> **No reverse proxy is deployed.** Every service above publishes its own port directly and
> speaks plain HTTP, RTMP, RTSP and MQTT. The target is a cloud L4 load balancer with
> Traefik behind it terminating HTTPS/WSS for the portal, HLS and WHEP, and cert-manager
> issuing for all three terminators — see §7 and §8 of
> [CLOUD_ARCHITECTURE.md](CLOUD_ARCHITECTURE.md). None of it exists yet, so run the stack
> on a trusted network or a VPN.

### Recording

MediaMTX records the annotated `annot` stream directly as it is received. When a segment is complete, MediaMTX calls a webhook on the `recorder` sidecar, which uploads the file according to `RECORDING_STORE_SERVICE` in the root `.env`.

---

## Prerequisites

**Comms machine:**

- Docker with the Compose plugin

**App machine:**

- Docker with the Compose plugin
- NVIDIA Container Toolkit (`nvidia-docker2` or `--gpus` support)
- NVIDIA MPS (required for danger detection, to share the GPU between YOLO and TensorRT processes):

  ```bash
  sudo nvidia-cuda-mps-control -d          # start MPS daemon on the host
  # ... run the app ...
  echo quit | sudo nvidia-cuda-mps-control  # stop when done
  ```

---

## Configuration

### Root `.env` — comms settings

Read by `docker-compose.yml` for variable substitution. Edit on the comms machine before starting:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `DB_HOST` | — | Partner database host (required) |
| `DB_PORT` | `5432` | Partner database port |
| `DB_NAME` | — | Database name (required) |
| `DB_WORKER_NAME` | — | Database username (required) |
| `DB_WORKER_PASSWORD` | — | Database password (required) |
| `DB_SERVICE` | `postgresql` | Database engine |
| `WS_PORT` | `8765` | External port for the WebSocket alert stream |
| `RECORDING_STORE_SERVICE` | `local` | Recording upload backend: `local`, `azure`, or `aws` |
| `RECORDING_DELETE_LOCAL_ON_SUCCESS` | `false` | Delete the local segment file after a successful remote upload |
| `RECORDING_AZURE_*` | — | Azure Blob Storage credentials (required when service=azure) |
| `RECORDING_AWS_*` | — | AWS S3 credentials (required when service=aws) |

### `app/.env` — pipeline settings

Read by `docker run --env-file app/.env`. Edit on the app machine before starting. Key settings:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `VIDEO_STREAM_READER_HOST` | `localhost` | Comms host IP or hostname (MediaMTX RTSP) |
| `TELEMETRY_LISTENER_HOST` | `localhost` | Comms host IP or hostname (Mosquitto MQTT) |
| `VIDEO_OUT_STREAM_HOST` | `localhost` | Comms host IP or hostname (MediaMTX RTMP) |
| `WS_SERVER_URL` | `http://localhost:8001` | ws-server HTTP API endpoint |
| `DB_WRITER_URL` | `http://localhost:8002` | db-writer HTTP API endpoint |
| `LOG_LEVEL` | `WARNING` | Pipeline worker log level. `INFO` turns on the per-stage diagnostics (MQTT subscriptions, telemetry matching, geo setup) that WARNING drops |

All three `HOST` variables and the two URLs must point to the comms machine. For local testing (both stacks on the same machine) the `localhost` defaults work as-is.

Additional groups in `app/.env`:

- **General**: `ALERTS_COOLDOWN_SECONDS`, `ALERTS_JPEG_COMPRESSION_QUALITY`
- **Drone hardware**: sensor dimensions (default: DJI Mavic 3 Enterprise)
- **Danger detection**: `SAFETY_RADIUS_M`, `SLOPE_ANGLE_THRESHOLD`, `GEOFENCING_VERTEXES`
- **Health monitoring anomaly detector**: thresholds and model parameters
- **Database credentials**: `DB_USERNAME`, `DB_PASSWORD` (end-user identity forwarded to db-writer)

---

## Quick Start

### Split deployment (comms VM + app machine)

**On the comms machine:**

```bash
# 1. Edit .env with your values (DB_*, ACME_EMAIL, RECORDING_*, WS_PORT)
# 2. Start the communication stack
docker compose -f docker-compose.yml up --build -d
```

**On the app machine:**

```bash
# 1. Edit app/.env — set HOST variables and URLs to the comms machine IP
# 2. Place DEM files in dem/ (danger detection only)
# 3. Build the image
docker build -t agrarian-app -f app/Dockerfile .
# 4. Run the app
docker run -d \
  --name agrarian \
  --restart unless-stopped \
  --env-file app/.env \
  --shm-size=256m \
  --gpus all \
  agrarian-app
```

### Local deployment (both on one machine)

```bash
# 1. Edit .env and app/.env (HOST variables can stay as localhost)
# 2. Start the comms stack first
docker compose -f docker-compose.yml up --build -d
# 3. Once comms services are healthy, build and run the app
docker build -t agrarian-app -f app/Dockerfile .
docker run -d \
  --name agrarian \
  --restart unless-stopped \
  --env-file app/.env \
  --shm-size=256m \
  --gpus all \
  agrarian-app
```

Set `APP_MODE` in `app/.env` to `danger_detection` or `health_monitoring`.

---

## Running with a TensorRT Engine

Place the compiled `.engine` file in the `engine/` directory on the app machine before starting:

```text
engine/
  detection_1280_720_yolo11m.engine   # health monitoring
  <detector>.engine                   # danger detection (optional)
  <segmenter>.engine                  # danger detection (optional)
```

The app detects engine files at startup and switches to engine mode automatically. Without an engine file, the `.pt`/`.onnx` checkpoint bundled in the image is used.

---

## Network

### Comms stack — ports exposed by `docker-compose.yml`

| Port | Protocol | Direction | Purpose |
| ---- | -------- | --------- | ------- |
| 8554 | RTSP | inbound | MediaMTX: drone video publish + app raw stream pull |
| 1935 | RTMP | inbound | MediaMTX: app annotated stream push |
| 8888 | HTTPS | inbound | Traefik → MediaMTX: HLS playback |
| 8889 | HTTPS | inbound | Traefik → MediaMTX: WebRTC/WHEP signalling |
| 8189 | UDP | inbound | MediaMTX: WebRTC media (DTLS-SRTP, never proxied) |
| 8189 | TCP | inbound | MediaMTX: WebRTC media over ICE-TCP, for networks that block UDP |
| 1883 | MQTT | inbound | Mosquitto: drone telemetry + app subscription |
| 443 | HTTPS | inbound | Traefik → portal: the account pages |
| `WS_PORT` | WSS | inbound | Traefik → ws-server: WebSocket alert stream |
| 8001 | HTTP | inbound | ws-server HTTP API (app POSTs alerts here) |
| 8002 | HTTP | inbound | db-writer HTTP API (app POSTs alerts here) |

8001 and 8002 are internal-only in the target topology; they are published here for the
split deployment below, where the app runs on a separate machine.

**Run `./scripts/generate_local_certs.sh` once before `docker compose up`.** Traefik
terminates TLS for the portal, HLS, WHEP and the WebSocket, and it needs a certificate;
that script issues one from a local CA, which stands in for cert-manager until a real
hostname exists. Trust `certificates/ca/ca.crt` in the browser to avoid a warning page.

The drone-facing ports — RTSP 8554, RTMP 1935 and MQTT 1883 — are **still in the clear**.
RTMPS, RTSPS and MQTTS are the next piece of the ingress tier.

### Access URLs

The portal composes the playback URLs itself, with a per-flight token attached — sign in at
`https://<comms-host>/` and use the **Watch** button rather than building these by hand.
Listed for debugging:

| Resource | URL |
| -------- | --- |
| Portal | `https://<comms-host>/` |
| HLS playback | `https://<comms-host>:8888/<output-path>/index.m3u8?jwt=<viewer-token>` |
| WebRTC playback | `https://<comms-host>:8889/<output-path>/whep?jwt=<viewer-token>` |
| WebSocket alerts | `wss://<comms-host>:${WS_PORT}/?token=<viewer-token>` |

`<output-path>` is the per-flight path assigned when the flight opens; a viewer token is
scoped to one flight and expires. Both come from the portal.

---

## Outputs

**Alert log** — written per session inside the `app` container (bind-mount if you need it on the host). One `.log` file per session, named by start timestamp:

```text
20260525_143012.log
```

**Process logs** — one file per pipeline stage, written to `./logs/` in the container (bind-mount as needed).

**Video recordings** — written by MediaMTX to the `recordings` Docker volume on the comms machine. The `recorder` sidecar uploads each completed segment to the configured backend. When `RECORDING_STORE_SERVICE=local`, segments remain on the volume indefinitely; for `azure` or `aws`, the file is optionally deleted after upload (`RECORDING_DELETE_LOCAL_ON_SUCCESS=true`).

---

## Shared Memory

The app container requires 256 MB of shared memory for the POSIX SHM frame buffers, passed via `--shm-size=256m` in the `docker run` command.

---

## MQTT Certificates (MQTTS)

When `TELEMETRY_LISTENER_PROTOCOL=mqtts`, the app expects a CA certificate at `certificates/mqtt/` inside the container. Add a bind-mount to the `docker run` command:

```bash
-v ./certificates/mqtt:/app/certificates/mqtt:ro
```
