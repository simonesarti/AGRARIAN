# AGRARIAN

Drone-based livestock monitoring pipeline with two operating modes, selected at runtime via the `APP_MODE` environment variable:

- **`danger_detection`** — detects animals and obstacles in the flight path, uses a DEM for terrain-aware safety assessment
- **`health_monitoring`** — tracks animals over time and flags behavioural anomalies

---

## Prerequisites

- Docker with NVIDIA Container Toolkit (`nvidia-docker2` or `--gpus` support)
- A `.env` file — copy `.env.example` and fill in your deployment values:

  ```bash
  cp .env.example .env
  ```

---

## Building the image

```bash
docker build -t agrarian .
```

---

## Running — Danger Detection

### Danger detection: required directories

```bash
mkdir -p logs processing_results dem certificates/mqtt
```

Place your DEM files in `dem/`:

```
dem/
  dem.tif
  dem_mask.tif
```

For MQTTS (optional): place the broker CA certificate in `certificates/mqtt/`.

### Danger detection: run command

```bash
docker run --rm \
  --gpus all \
  --env-file .env \
  -e APP_MODE=danger_detection \
  -p 8443:8443 \
  -v ./logs:/app/logs \
  -v ./processing_results:/app/processing_results \
  -v ./dem:/app/dem \
  -v ./certificates/mqtt:/app/certificates/mqtt \
  agrarian
```

### Network

| Port | Protocol | Role                  | Purpose                                          |
|------|----------|-----------------------|--------------------------------------------------|
| 8554 | RTSP     | outbound (client)     | Container reads video from media server          |
| 1883 | MQTT     | outbound (client)     | Container reads telemetry from MQTT broker       |
| 1935 | RTMP     | outbound (client)     | Container pushes annotated stream to media server|
| 8443 | WSS      | **inbound (server)**  | UI connects to container's WebSocket alert server|

Only port 8443 is published with `-p` because it is the only port the container listens on. The other three are outbound connections the container makes to external services — no `-p` needed for those.

Adjust port numbers to match your `.env` if you changed the defaults.

---

## Running — Health Monitoring

### Health monitoring: required directories

```bash
mkdir -p logs processing_results certificates/mqtt
```

For MQTTS (optional): place the broker CA certificate in `certificates/mqtt/`.

### Health monitoring: run command

```bash
docker run --rm \
  --gpus all \
  --env-file .env \
  -e APP_MODE=health_monitoring \
  -p 8443:8443 \
  -v ./logs:/app/logs \
  -v ./processing_results:/app/processing_results \
  -v ./certificates/mqtt:/app/certificates/mqtt \
  agrarian
```

No DEM volume is needed for health monitoring. The same network table above applies.

---

## Volume reference

| Host path               | Container path              | Contents                                             |
|-------------------------|-----------------------------|------------------------------------------------------|
| `./logs/`               | `/app/logs/`                | Per-process log files (one per pipeline stage)       |
| `./processing_results/` | `/app/processing_results/`  | Recorded session videos (`.mp4`) and alert logs      |
| `./dem/`                | `/app/dem/`                 | `dem.tif` and `dem_mask.tif` — danger detection only |
| `./certificates/mqtt/`  | `/app/certificates/mqtt/`   | MQTT broker CA certificate — only needed for MQTTS   |

All directories are read or written at runtime; none are baked into the image.

---

## Outputs

After a session, the following files are written to the mounted volumes.

**`./logs/`** — one file per pipeline process, e.g.:
```
main.log
stream_video_in.log
frame_telemetry_combiner.log
animals_detection.log
danger_segmentation.log
danger_geo.log
danger_annotation.log
alert_out.log
video_out.log
```

**`./processing_results/`** — one set of files per session, named by start timestamp:
```
20260525_143012.mp4     # annotated video recording
20260525_143012.log     # alert event log for the session
```
