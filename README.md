# AGRARIAN — PILOT 1

![AGRARIAN](assets/agrarian.png)

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

### Danger detection: volumes

All data is exchanged with the container via bind mounts. The host-side paths are conventions; any path works as long as it maps to the correct container-side path.

| Container path              | R/W | Required   | Contents                                              |
|-----------------------------|-----|------------|-------------------------------------------------------|
| `/app/dem`                  | R   | Yes        | `dem.tif` and `dem_mask.tif`                          |
| `/app/logs`                 | W   | No         | Per-process log files, one per pipeline stage         |
| `/app/processing_results`   | W   | No         | Session video (`.mp4`) and alert log (`.log`)         |
| `/app/certificates/mqtt`    | R   | MQTTS only | CA certificate for the MQTT broker                    |

Populate the `dem` directory on the host before starting the container:

```text
<host-dem-path>/
  dem.tif
  dem_mask.tif
```

### Danger detection: run command

```bash
docker run --rm \
  --name agrarian \
  --gpus all \
  --shm-size=128m \
  --env-file .env \
  -e APP_MODE=danger_detection \
  -p 8443:8443 \
  -v /path/to/dem:/app/dem \
  -v /path/to/logs:/app/logs \
  -v /path/to/processing_results:/app/processing_results \
  -v /path/to/certificates/mqtt:/app/certificates/mqtt \
  agrarian
```

The last `-v` line (certificates) is only needed when `TELEMETRY_LISTENER_PROTOCOL=mqtts`.

### Danger detection: network

| Port        | Protocol | Role                 | Purpose                                                    |
|-------------|----------|----------------------|------------------------------------------------------------|
| 8554        | RTSP     | outbound (client)    | Container reads video from media server                    |
| 1883        | MQTT     | outbound (client)    | Container reads telemetry from MQTT broker                 |
| 1935        | RTMP     | outbound (client)    | Container pushes annotated stream to media server          |
| 8443        | WSS      | **inbound (server)** | UI connects to container's WebSocket alert server          |
| 5432 / 3306 | TCP      | outbound (client)    | Alert storage — PostgreSQL / MySQL (disabled if unset)     |

Only port 8443 is published with `-p` — it is the only port the container listens on. All others are outbound connections to external services. Adjust port numbers to match your `.env` if you changed the defaults.

---

## Running — Health Monitoring

### Health monitoring: volumes

Health monitoring does not use telemetry — no MQTT broker or certificates are needed.

| Container path              | R/W | Required | Contents                                              |
|-----------------------------|-----|----------|-------------------------------------------------------|
| `/app/logs`                 | W   | No       | Per-process log files, one per pipeline stage         |
| `/app/processing_results`   | W   | No       | Session video (`.mp4`) and alert log (`.log`)         |

### Health monitoring: run command

```bash
docker run --rm \
  --name agrarian \
  --gpus all \
  --shm-size=128m \
  --env-file .env \
  -e APP_MODE=health_monitoring \
  -p 8443:8443 \
  -v /path/to/logs:/app/logs \
  -v /path/to/processing_results:/app/processing_results \
  agrarian
```

### Health monitoring: network

| Port        | Protocol | Role                 | Purpose                                                    |
|-------------|----------|----------------------|------------------------------------------------------------|
| 8554        | RTSP     | outbound (client)    | Container reads video from media server                    |
| 1935        | RTMP     | outbound (client)    | Container pushes annotated stream to media server          |
| 8443        | WSS      | **inbound (server)** | UI connects to container's WebSocket alert server          |
| 5432 / 3306 | TCP      | outbound (client)    | Alert storage — PostgreSQL / MySQL (disabled if unset)     |

---

## Container Networking

When multiple containers are running on the default bridge network, use their IP addresses to communicate between them (the default bridge does not support DNS name resolution).

Find the IP of every running container:

```bash
docker inspect -f '{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $(docker ps -q)
```

Or for a single container by name:

```bash
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' <container-name>
```

Use the resolved IP in `.env` for `VIDEO_STREAM_READER_HOST`, `TELEMETRY_LISTENER_HOST`, `VIDEO_OUT_STREAM_HOST`, and `DB_HOST` as appropriate.

---

## Local Testing

Commands for running both apps from the project directory, mounting subdirectories of the repo as volumes and using the local `.env` file. Logs and outputs land directly in the project tree for easy inspection.

### Test: danger detection

Place `dem.tif` and `dem_mask.tif` in the project's `dem/` directory before running.

```bash
docker run --rm \
  --name agrarian \
  --gpus all \
  --shm-size=128m \
  --env-file .env \
  -e APP_MODE=danger_detection \
  -p 8443:8443 \
  -v ./dem:/app/dem \
  -v ./logs:/app/logs \
  -v ./processing_results:/app/processing_results \
  agrarian
```

Add `-v ./certificates/mqtt:/app/certificates/mqtt` if testing with `TELEMETRY_LISTENER_PROTOCOL=mqtts`.

### Test: health monitoring

```bash
docker run --rm \
  --name agrarian \
  --gpus all \
  --shm-size=128m \
  --env-file .env \
  -e APP_MODE=health_monitoring \
  -p 8443:8443 \
  -v ./logs:/app/logs \
  -v ./processing_results:/app/processing_results \
  agrarian
```

---

## Outputs

After a session, the following files are written to the mounted volumes.

**`/app/logs`** — one file per pipeline process:

```text
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

**`/app/processing_results`** — one set of files per session, named by start timestamp:

```text
20260525_143012.mp4     # annotated video recording
20260525_143012.log     # alert event log for the session
```
