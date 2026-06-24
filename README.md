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

The full stack is defined in [docker-compose.yml](docker-compose.yml). All services run on an isolated internal bridge network (`session-net`).

| Service | Role |
| ------- | ---- |
| **traefik** | Reverse proxy; TLS termination via Let's Encrypt; routes HLS, WebRTC, and WebSocket traffic |
| **mediamtx** | Video ingestion from drone (RTSP); re-publishes annotated stream (RTMP); records the annotated stream to the `recordings` volume |
| **mosquitto** | MQTT broker; receives drone telemetry consumed by the app |
| **postgres** | Alert persistence database |
| **db-writer** | Receives alert POST requests from the app and writes them to PostgreSQL; decouples the app from DB write latency |
| **ws-server** | Maintains a WebSocket connection to the viewer UI; receives alert events from the app and pushes them in real time |
| **recorder** | Receives a webhook from MediaMTX on each completed recording segment and uploads it to the configured storage backend (local volume, Azure Blob Storage, or AWS S3) |
| **app** | Core GPU processing pipeline; consumes video and telemetry, produces annotated stream and structured alerts |

### Recording

MediaMTX records the annotated `annot` stream directly as it is received, removing the need for the app to write video files. When a segment is complete, MediaMTX calls a webhook on the `recorder` sidecar, which uploads the file according to `RECORDING_STORE_SERVICE` in the root `.env`.

---

## Prerequisites

- Docker with the Compose plugin
- NVIDIA Container Toolkit (`nvidia-docker2` or `--gpus` support) on the host
- NVIDIA MPS (required for danger detection, to share the GPU between YOLO and TensorRT processes):

  ```bash
  sudo nvidia-cuda-mps-control -d          # start MPS daemon on the host
  # ... run the stack ...
  echo quit | sudo nvidia-cuda-mps-control  # stop when done
  ```

---

## Configuration

Two configuration files are required.

### Root `.env`

Compose-level settings — variables that must drive both port bindings in `docker-compose.yml` and container environment variables. Edit before running:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `WS_PORT` | `8765` | External port for the WebSocket alert stream |
| `ACME_EMAIL` | — | Email for Let's Encrypt certificate registration (required for WSS in production) |
| `RECORDING_STORE_SERVICE` | `local` | Recording upload backend: `local`, `azure`, or `aws` |
| `RECORDING_DELETE_LOCAL_ON_SUCCESS` | `false` | Delete the local segment file after a successful remote upload |
| `RECORDING_AZURE_*` | — | Azure Blob Storage credentials (required when service=azure) |
| `RECORDING_AWS_*` | — | AWS S3 credentials (required when service=aws) |

### `app/.env`

Pipeline configuration — read by the `app` container at startup. Key groups:

- **General**: `ALERTS_COOLDOWN_SECONDS`, `ALERTS_JPEG_COMPRESSION_QUALITY`
- **Drone hardware**: sensor dimensions (default: DJI Mavic 3 Enterprise)
- **Video stream reader**: `VIDEO_STREAM_READER_HOST/PORT/STREAM_KEY` and protocol
- **Telemetry/MQTT**: `TELEMETRY_LISTENER_HOST/PORT` and protocol
- **Danger detection**: `SAFETY_RADIUS_M`, `SLOPE_ANGLE_THRESHOLD`, `GEOFENCING_VERTEXES`
- **Health monitoring anomaly detector**: thresholds and model parameters
- **Video stream output**: `VIDEO_OUT_STREAM_HOST/PORT/STREAM_KEY` (default: `mediamtx:1935/annot`)
- **Database credentials**: `DB_USERNAME`, `DB_PASSWORD` (end-user identity forwarded to db-writer)

`WS_SERVER_URL` and `DB_WRITER_URL` are hardcoded in `docker-compose.yml` as internal service URLs and do not appear in `app/.env`.

---

## Quick Start

```bash
# 1. Fill in configuration
#    Edit the root .env and app/.env with your deployment values.

# 2. Place DEM files (danger detection only)
#    Put dem.tif and dem_mask.tif in the dem/ directory.

# 3. Start the stack
docker compose up --build
```

Set `APP_MODE` in `app/.env` to `danger_detection` or `health_monitoring`.

---

## Running with a TensorRT Engine

Place the compiled `.engine` file in the `engine/` directory before starting:

```text
engine/
  detection_1280_720_yolo11m.engine   # health monitoring
  <detector>.engine                   # danger detection (optional)
  <segmenter>.engine                  # danger detection (optional)
```

The app detects engine files at startup and switches to engine mode automatically. Without an engine file, the `.pt`/`.onnx` checkpoint bundled in the image is used.

---

## Network

Ports exposed externally by the stack:

| Port | Protocol | Direction | Purpose |
| ---- | -------- | --------- | ------- |
| 80 | HTTP | inbound | Traefik (Let's Encrypt HTTP challenge; redirects to 443 in production) |
| 443 | HTTPS/WSS | inbound | Traefik: HLS/WebRTC video playback + WSS alerts (production) |
| 8080 | HTTP | inbound | Traefik dashboard (development only) |
| 8554 | RTSP | inbound | MediaMTX: drone video publish |
| 1935 | RTMP | inbound | MediaMTX: app annotated stream publish |
| 8889 | WebRTC | inbound | MediaMTX: viewer WebRTC playback |
| 1883 | MQTT | inbound | Mosquitto: drone telemetry |
| `WS_PORT` | WS | inbound | WebSocket alert stream (direct, without Traefik) |

Access URLs (development, via Traefik on localhost):

| Resource | URL |
| -------- | --- |
| HLS playback | `http://localhost/hls/annot/index.m3u8` |
| WebRTC playback | `http://localhost/webrtc/annot/whep` |
| WebSocket alerts (WS) | `ws://localhost/ws` |
| WebSocket alerts (WSS, production) | `wss://<domain>/ws` |
| WebSocket alerts (direct) | `ws://localhost:${WS_PORT}` |
| Traefik dashboard | `http://localhost:8080` |

---

## Outputs

**Alert log** — written per session to `app/processing_results/` inside the `app` container (bind-mount if you need it on the host). One `.log` file per session, named by start timestamp:

```text
20260525_143012.log
```

**Process logs** — one file per pipeline stage, written to `./logs/` in the container (bind-mount as needed).

**Video recordings** — written by MediaMTX to the `recordings` Docker volume. The `recorder` sidecar uploads each completed segment to the configured backend. When `RECORDING_STORE_SERVICE=local`, segments remain on the volume indefinitely; for `azure` or `aws`, the file is optionally deleted after upload (`RECORDING_DELETE_LOCAL_ON_SUCCESS=true`).

---

## Shared Memory

The app container requires at least 256 MB of shared memory for the POSIX SHM frame buffers. This is configured in `docker-compose.yml` (`shm_size: "256m"`) and applies automatically when using Compose. If running the container standalone, pass `--shm-size=256m`.

---

## MQTT Certificates (MQTTS)

When `TELEMETRY_LISTENER_PROTOCOL=mqtts`, the app expects a CA certificate at `certificates/mqtt/` inside the container. Bind-mount the directory:

```yaml
# add to the app service in docker-compose.yml
volumes:
  - ./certificates/mqtt:/app/certificates/mqtt:ro
```
