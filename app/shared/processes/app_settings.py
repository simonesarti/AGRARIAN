import math
import re
from typing import Any, Literal, Optional

from pydantic import Field, PositiveFloat, PositiveInt, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.shared.processes.constants import (
    ALERTS_COOLDOWN_SECONDS,
    ALERTS_JPEG_COMPRESSION_QUALITY,
    HM_ANOMALY_AE_THRESHOLD,
    HM_ANOMALY_MIN_ANOMALY_DURATION,
    HM_ANOMALY_REQUIRE_BOTH,
    HM_ANOMALY_SMOOTHING_WINDOW,
    HM_ANOMALY_SOCIAL_EMA_ALPHA,
    HM_ANOMALY_SOCIAL_MIN_HERD,
    HM_ANOMALY_SOCIAL_MIN_UPDATES,
    HM_ANOMALY_SOCIAL_THRESHOLD,
    HM_ANOMALY_USE_AE,
    HM_ANOMALY_USE_SOCIAL,
DRONE_SENSOR_HEIGHT_MM,
    DRONE_SENSOR_HEIGHT_PIXELS,
    DRONE_SENSOR_WIDTH_MM,
    DRONE_SENSOR_WIDTH_PIXELS,
    DRONE_TRUE_FOCAL_LEN_MM,
    SAFETY_RADIUS_M,
    SLOPE_ANGLE_THRESHOLD,
    TELEMETRY_LISTENER_HOST,
    TELEMETRY_LISTENER_PORT,
    TELEMETRY_LISTENER_QOS_LEVEL,
    VIDEO_OUT_STREAM_HOST,
    VIDEO_OUT_STREAM_PORT,
    VIDEO_STREAM_READER_HOST,
    VIDEO_STREAM_READER_PORT,
)
from app.shared.processes.stream_urls import build_stream_url, redact_stream_url


class AppSettings(BaseSettings):
    """
    Single source of truth for deployment-varying pipeline configuration.

    Values are read from environment variables (case-insensitive) and from a
    .env file if present.  The field name maps 1-to-1 to the env var name:
    e.g. field `db_writer_url` reads DB_WRITER_URL.

    env_ignore_empty=True means an empty string in the environment is treated
    the same as "not set" and causes the field default to be used instead.

    Internal tuning constants (queue sizes, timeouts, retry counts) live in
    constants.py and are not configurable via environment variables.

    ---

    The environment has two halves, and telling them apart is the point of the
    section headings below.

    **Deployment settings** an operator sets once (model thresholds, drone optics,
    service hostnames). These are configured on the orchestrator and forwarded to
    every flight container unchanged.

    **Flight identity** — FLIGHT_ID, PUBLISHER_TOKEN and the two stream paths —
    injected per container by the orchestrator when a drone goes live. An operator
    never sets these, and per-flight values override anything in the base
    environment, so a stray VIDEO_OUT_STREAM_STREAM_KEY cannot redirect a tenant's
    annotated video. There is no email or password anywhere in here: this container
    processes untrusted video and holds no reusable account credential.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # ------------------------------------------------------------------ #
    # FLIGHT IDENTITY  —  injected by the orchestrator, never operator-set
    # ------------------------------------------------------------------ #

    # The flight every alert and every frame produced by this container belongs to.
    flight_id: int = Field(ge=1)

    # The one credential this container holds. Minted by db-writer for this flight
    # alone and returned exactly once, so a leak buys nothing but this flight. It
    # authorises four things and nothing else: reading the drone's ingest path,
    # publishing the annotated output, writing alerts for this flight_id, and
    # subscribing to this flight's telemetry topics.
    publisher_token: SecretStr

    # ------------------------------------------------------------------ #
    # GENERAL
    # ------------------------------------------------------------------ #

    alerts_cooldown_seconds: PositiveFloat = ALERTS_COOLDOWN_SECONDS

    # ------------------------------------------------------------------ #
    # DRONE HARDWARE
    # ------------------------------------------------------------------ #

    drone_true_focal_len_mm:    PositiveFloat = DRONE_TRUE_FOCAL_LEN_MM
    drone_sensor_width_mm:      PositiveFloat = DRONE_SENSOR_WIDTH_MM
    drone_sensor_height_mm:     PositiveFloat = DRONE_SENSOR_HEIGHT_MM
    drone_sensor_width_pixels:  PositiveInt   = DRONE_SENSOR_WIDTH_PIXELS
    drone_sensor_height_pixels: PositiveInt   = DRONE_SENSOR_HEIGHT_PIXELS

    # ------------------------------------------------------------------ #
    # DANGER DETECTION
    # ------------------------------------------------------------------ #

    safety_radius_m:       PositiveFloat = SAFETY_RADIUS_M
    slope_angle_threshold: float         = Field(default=SLOPE_ANGLE_THRESHOLD, ge=0, lt=90)
    # Parsed from "(lon1, lat1), (lon2, lat2), ..." — leave empty/unset to disable geofencing
    geofencing_vertexes: Optional[list[tuple[float, float]]] = None

    # ------------------------------------------------------------------ #
    # HEALTH MONITORING — ANOMALY DETECTION
    # ------------------------------------------------------------------ #

    hm_anomaly_use_ae:              bool           = HM_ANOMALY_USE_AE
    hm_anomaly_use_social:          bool           = HM_ANOMALY_USE_SOCIAL
    hm_anomaly_ae_threshold:        PositiveFloat  = HM_ANOMALY_AE_THRESHOLD
    hm_anomaly_social_threshold:    PositiveFloat  = HM_ANOMALY_SOCIAL_THRESHOLD
    hm_anomaly_smoothing_window:    PositiveInt    = HM_ANOMALY_SMOOTHING_WINDOW
    hm_anomaly_min_anomaly_duration: PositiveInt   = HM_ANOMALY_MIN_ANOMALY_DURATION
    hm_anomaly_social_ema_alpha:    PositiveFloat  = HM_ANOMALY_SOCIAL_EMA_ALPHA
    hm_anomaly_social_min_updates:  PositiveInt    = HM_ANOMALY_SOCIAL_MIN_UPDATES
    hm_anomaly_social_min_herd:     PositiveInt    = HM_ANOMALY_SOCIAL_MIN_HERD
    hm_anomaly_require_both:        bool           = HM_ANOMALY_REQUIRE_BOTH

    # ------------------------------------------------------------------ #
    # VIDEO STREAM READER
    # ------------------------------------------------------------------ #

    video_stream_reader_protocol:              Literal["rtsp", "rtmp", "rtmps", "rtsps"] = "rtsp"
    video_stream_reader_host:                  str           = VIDEO_STREAM_READER_HOST
    video_stream_reader_port:                  int           = Field(default=VIDEO_STREAM_READER_PORT, ge=1, le=65535)
    # The media-server path to read, e.g. `in/<stream_key>`. Injected per flight; no
    # default, because a wrong default here means silently reading nothing.
    #
    # There is no username/password pair any more. The drone's ingest path is not
    # protected by a pre-shared credential the operator configures — it is protected
    # by the publisher token above, which names this flight. That also means rotating
    # a stream key or revoking a stream takes effect without reconfiguring anything.
    video_stream_reader_stream_key:            str

    # ------------------------------------------------------------------ #
    # TELEMETRY / MQTT
    # ------------------------------------------------------------------ #

    telemetry_listener_protocol:              Literal["mqtt", "mqtts"] = "mqtt"
    telemetry_listener_host:                  str              = TELEMETRY_LISTENER_HOST
    telemetry_listener_port:                  int              = Field(default=TELEMETRY_LISTENER_PORT, ge=1, le=65535)
    # There is no separate username/password pair. Mosquitto's ACL plugin
    # authorises this container with the publisher token above, the same
    # credential that already authorises the video ingest read and the
    # annotated-output publish — this is a third thing it authorises, not a new
    # credential. The stream this flight was opened on, e.g. `<stream_key>`.
    # Injected per flight; no default, for the same reason
    # video_stream_reader_stream_key has none: a wrong default here means
    # silently subscribing to nothing, or — worse — to somebody else's topic.
    telemetry_listener_stream_key:            str
    telemetry_listener_qos_level: Literal[0, 1, 2] = TELEMETRY_LISTENER_QOS_LEVEL

    # ------------------------------------------------------------------ #
    # ALERTS WRITER
    # ------------------------------------------------------------------ #

    alerts_jpeg_compression_quality: int = Field(default=ALERTS_JPEG_COMPRESSION_QUALITY, ge=0, le=100)

    # ------------------------------------------------------------------ #
    # WEBSOCKET SERVER SIDECAR
    # ------------------------------------------------------------------ #

    # URL of the ws-server sidecar HTTP API (e.g. http://ws-server:8000).
    # Authorised with the per-flight publisher token, so there is no pre-shared
    # secret to configure here.
    ws_server_url: str

    # ------------------------------------------------------------------ #
    # DATABASE
    # ------------------------------------------------------------------ #

    # URL of the db-writer sidecar HTTP API (e.g. http://db-writer:8000).
    # The sidecar holds the privileged DB credentials. Nothing identifying an end user
    # goes here: the app used to send DB_USERNAME/DB_PASSWORD to /session/start to
    # open its own flight, which put a reusable account credential inside this
    # container. The orchestrator opens the flight now and injects the result.
    db_writer_url: str

    # ------------------------------------------------------------------ #
    # VIDEO STREAM OUTPUT (RTMP → media server)
    # ------------------------------------------------------------------ #

    video_out_stream_protocol:               Literal["rtmp", "rtmps"] = "rtmp"
    video_out_stream_host:                   str           = VIDEO_OUT_STREAM_HOST
    video_out_stream_port:                   int           = Field(default=VIDEO_OUT_STREAM_PORT, ge=1, le=65535)
    # The media-server path to publish to, e.g. `out/<public_uuid>`. Injected per
    # flight. Authorised by the publisher token, like the ingest path above.
    video_out_stream_stream_key:             str

    # ================================================================== #
    # FIELD VALIDATORS
    # ================================================================== #

    @field_validator(
        "video_stream_reader_protocol",
        "telemetry_listener_protocol",
        "video_out_stream_protocol",
        mode="before",
    )
    @classmethod
    def _lowercase(cls, v: Any) -> Any:
        return v.lower() if isinstance(v, str) else v

    @field_validator("telemetry_listener_qos_level", mode="before")
    @classmethod
    def _coerce_qos(cls, v: Any) -> Any:
        return int(v) if isinstance(v, str) else v

    @field_validator("geofencing_vertexes", mode="before")
    @classmethod
    def _parse_geofencing(cls, v: Any) -> Optional[list[tuple[float, float]]]:
        if v is None:
            return None
        s = str(v).strip()
        if s.lower() in ("", "none"):
            return None
        pattern = r"\(\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*\)"
        matches = re.findall(pattern, s)
        if len(matches) < 3:
            raise ValueError(
                "GEOFENCING_VERTEXES must contain at least 3 (longitude, latitude) pairs, "
                f"got {len(matches)}. Expected format: '(lon1, lat1), (lon2, lat2), ...'"
            )
        result: list[tuple[float, float]] = []
        for lon_s, lat_s in matches:
            lon, lat = float(lon_s), float(lat_s)
            if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                raise ValueError(
                    f"Coordinate ({lon}, {lat}) is out of valid range "
                    "(longitude: -180..180, latitude: -90..90)."
                )
            result.append((lon, lat))
        return result

    # ================================================================== #
    # CROSS-FIELD VALIDATION
    # ================================================================== #

    @model_validator(mode="after")
    def _validate_all(self) -> "AppSettings":

        # --- sensor aspect ratio ---
        phys = self.drone_sensor_width_mm / self.drone_sensor_height_mm
        pix  = self.drone_sensor_width_pixels / self.drone_sensor_height_pixels
        if not math.isclose(phys, pix, rel_tol=1e-3):
            raise ValueError(
                f"Drone sensor aspect ratio mismatch: physical={phys:.4f}, "
                f"pixel={pix:.4f}. Verify DRONE_SENSOR_*_MM and DRONE_SENSOR_*_PIXELS."
            )

        # --- stream paths ---
        # Reading and writing the same path would make the pipeline publish into its
        # own input. MediaMTX would accept it (both are authorised by the same token)
        # and the result is a feedback loop that looks like a decoder fault.
        if self.video_stream_reader_stream_key.strip("/") == self.video_out_stream_stream_key.strip("/"):
            raise ValueError(
                "VIDEO_STREAM_READER_STREAM_KEY and VIDEO_OUT_STREAM_STREAM_KEY are the "
                f"same path ('{self.video_stream_reader_stream_key}'). The pipeline would "
                "publish its annotated output into its own input."
            )

        return self

    # ================================================================== #
    # STREAM URLS
    # ================================================================== #
    # Deliberately NOT computed_field: these carry the publisher token, and a
    # computed_field would place it in model_dump() output for anything that ever
    # serialises the settings. The *_url_redacted variants are the loggable ones,
    # and those are computed fields precisely so a dump prefers them.

    @property
    def video_stream_reader_url(self) -> str:
        """RTSP/RTMP URL for the drone video stream input. CONTAINS A CREDENTIAL."""
        return build_stream_url(
            self.video_stream_reader_protocol,
            self.video_stream_reader_host,
            self.video_stream_reader_port,
            self.video_stream_reader_stream_key,
            self.publisher_token.get_secret_value(),
        )

    @property
    def video_out_stream_url(self) -> str:
        """RTMP URL for the annotated output (FFmpeg → media server). CONTAINS A CREDENTIAL."""
        return build_stream_url(
            self.video_out_stream_protocol,
            self.video_out_stream_host,
            self.video_out_stream_port,
            self.video_out_stream_stream_key,
            self.publisher_token.get_secret_value(),
        )

    @computed_field
    @property
    def video_stream_reader_url_redacted(self) -> str:
        """Safe to log: same URL with the token replaced."""
        return redact_stream_url(self.video_stream_reader_url)

    @computed_field
    @property
    def video_out_stream_url_redacted(self) -> str:
        """Safe to log: same URL with the token replaced."""
        return redact_stream_url(self.video_out_stream_url)
