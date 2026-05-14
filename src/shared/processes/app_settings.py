import math
import re
from typing import Any, Literal, Optional

from pydantic import Field, PositiveFloat, PositiveInt, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.shared.processes.constants import (
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
    DB_HOST,
    DB_PORT,
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
    VIDEO_OUT_STORE_DELETE_LOCAL_ON_SUCCESS,
    VIDEO_OUT_STORE_LOCAL_TARGET_DIR,
    VIDEO_OUT_STREAM_HOST,
    VIDEO_OUT_STREAM_PORT,
    VIDEO_OUT_STREAM_STREAM_KEY,
    VIDEO_STREAM_READER_HOST,
    VIDEO_STREAM_READER_PORT,
    VIDEO_STREAM_READER_STREAM_KEY,
    WEBSOCKET_HOST,
    WEBSOCKET_PORT,
)


class AppSettings(BaseSettings):
    """
    Single source of truth for deployment-varying pipeline configuration.

    Values are read from environment variables (case-insensitive) and from a
    .env file if present.  The field name maps 1-to-1 to the env var name:
    e.g. field `db_service` reads DB_SERVICE.

    env_ignore_empty=True means an empty string in the environment is treated
    the same as "not set" and causes the field default to be used instead.

    Internal tuning constants (queue sizes, timeouts, retry counts) live in
    constants.py and are not configurable via environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_ignore_empty=True,
    )

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
    # DANGER DETECTION PARAMETERS
    # ------------------------------------------------------------------ #

    safety_radius_m:       PositiveFloat = SAFETY_RADIUS_M
    slope_angle_threshold: float         = Field(default=SLOPE_ANGLE_THRESHOLD, ge=0, le=90)
    # Parsed from "(lon1, lat1), (lon2, lat2), ..." — leave empty/unset to disable geofencing
    geofencing_vertexes: Optional[list[tuple[float, float]]] = None

    # ------------------------------------------------------------------ #
    # VIDEO STREAM READER
    # ------------------------------------------------------------------ #

    video_stream_reader_protocol:              Literal["rtsp", "rtmp", "rtmps", "rtsps"] = "rtsp"
    video_stream_reader_host:                  str           = VIDEO_STREAM_READER_HOST
    video_stream_reader_port:                  int           = Field(default=VIDEO_STREAM_READER_PORT, ge=1, le=65535)
    video_stream_reader_stream_key:            str           = VIDEO_STREAM_READER_STREAM_KEY
    video_stream_reader_username: Optional[str]       = None
    video_stream_reader_password: Optional[SecretStr] = None

    # ------------------------------------------------------------------ #
    # TELEMETRY / MQTT
    # ------------------------------------------------------------------ #

    telemetry_listener_protocol:              Literal["mqtt", "mqtts"] = "mqtt"
    telemetry_listener_host:                  str              = TELEMETRY_LISTENER_HOST
    telemetry_listener_port:                  int              = Field(default=TELEMETRY_LISTENER_PORT, ge=1, le=65535)
    telemetry_listener_username: Optional[str]       = None
    telemetry_listener_password: Optional[SecretStr] = None
    telemetry_listener_qos_level: Literal[0, 1, 2] = TELEMETRY_LISTENER_QOS_LEVEL

    # ------------------------------------------------------------------ #
    # ALERTS WRITER
    # ------------------------------------------------------------------ #

    alerts_jpeg_compression_quality: int = Field(default=ALERTS_JPEG_COMPRESSION_QUALITY, ge=0, le=100)

    # ------------------------------------------------------------------ #
    # WEBSOCKET SERVER
    # ------------------------------------------------------------------ #

    websocket_host: str = WEBSOCKET_HOST
    websocket_port: int = Field(default=WEBSOCKET_PORT, ge=1, le=65535)

    # ------------------------------------------------------------------ #
    # DATABASE
    # ------------------------------------------------------------------ #

    # Supported: postgresql, mysql, sqlite — leave empty/unset to disable
    db_service:                  Optional[Literal["postgresql", "mysql", "sqlite"]] = None
    db_host:                     str           = DB_HOST
    db_port:                     int           = Field(default=DB_PORT, ge=1, le=65535)
    db_worker_name:     Optional[str]       = None
    db_worker_password: Optional[SecretStr] = None
    db_username:        str                 = ""
    db_password:        SecretStr           = ""

    # ------------------------------------------------------------------ #
    # VIDEO STREAM OUTPUT (RTMP → media server)
    # ------------------------------------------------------------------ #

    video_out_stream_protocol:               Literal["rtmp", "rtmps"] = "rtmp"
    video_out_stream_host:                   str           = VIDEO_OUT_STREAM_HOST
    video_out_stream_port:                   int           = Field(default=VIDEO_OUT_STREAM_PORT, ge=1, le=65535)
    video_out_stream_stream_key:             str           = VIDEO_OUT_STREAM_STREAM_KEY
    video_out_stream_username: Optional[str]       = None
    video_out_stream_password: Optional[SecretStr] = None

    # ------------------------------------------------------------------ #
    # VIDEO STORAGE (cloud / local upload after recording)
    # ------------------------------------------------------------------ #

    # Service: azure, aws, local
    video_out_store_service:                 Literal["azure", "aws", "local"] = "local"
    video_out_store_delete_local_on_success: bool = VIDEO_OUT_STORE_DELETE_LOCAL_ON_SUCCESS
    # Azure Blob Storage
    video_out_store_azure_connection_string: Optional[SecretStr] = None
    video_out_store_azure_container_name:    Optional[str]       = None
    video_out_store_azure_blob_prefix:       str                 = ""
    # AWS S3
    video_out_store_aws_bucket_name:         Optional[str]       = None
    video_out_store_aws_key_prefix:          str                 = ""
    video_out_store_aws_access_key_id:       Optional[str]       = None
    video_out_store_aws_secret_access_key:   Optional[SecretStr] = None
    video_out_store_aws_region_name:         Optional[str]       = None
    # Local (testing / no-cloud fallback)
    video_out_store_local_target_dir: str = VIDEO_OUT_STORE_LOCAL_TARGET_DIR

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

    # ================================================================== #
    # FIELD VALIDATORS
    # ================================================================== #

    @field_validator(
        "video_stream_reader_protocol",
        "telemetry_listener_protocol",
        "video_out_stream_protocol",
        "video_out_store_service",
        mode="before",
    )
    @classmethod
    def _lowercase(cls, v: Any) -> Any:
        return v.lower() if isinstance(v, str) else v

    @field_validator("telemetry_listener_qos_level", mode="before")
    @classmethod
    def _coerce_qos(cls, v: Any) -> Any:
        return int(v) if isinstance(v, str) else v

    @field_validator("db_service", mode="before")
    @classmethod
    def _normalize_db_service(cls, v: Any) -> Optional[str]:
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "none")):
            return None
        return v.strip().lower()

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

        # --- video stream reader ---
        if self.video_stream_reader_protocol in ("rtmps", "rtsps"):
            if not (self.video_stream_reader_username and self.video_stream_reader_password):
                raise ValueError(
                    f"{self.video_stream_reader_protocol.upper()} requires "
                    "VIDEO_STREAM_READER_USERNAME and VIDEO_STREAM_READER_PASSWORD."
                )
        else:
            # Credentials are not used for non-secure protocols
            self.video_stream_reader_username = None
            self.video_stream_reader_password = None

        # --- telemetry / MQTT ---
        if self.telemetry_listener_protocol == "mqtts":
            if not (self.telemetry_listener_username and self.telemetry_listener_password):
                raise ValueError(
                    "MQTTS requires TELEMETRY_LISTENER_USERNAME and TELEMETRY_LISTENER_PASSWORD."
                )
        else:
            self.telemetry_listener_username = None
            self.telemetry_listener_password = None

        # --- video stream output ---
        if self.video_out_stream_protocol == "rtmps":
            if not (self.video_out_stream_username and self.video_out_stream_password):
                raise ValueError(
                    "RTMPS requires VIDEO_OUT_STREAM_USERNAME and VIDEO_OUT_STREAM_PASSWORD."
                )
        else:
            self.video_out_stream_username = None
            self.video_out_stream_password = None

        # --- database ---
        if self.db_service in ("postgresql", "mysql"):
            if not (self.db_worker_name and self.db_worker_password):
                raise ValueError(
                    f"{self.db_service.upper()} requires DB_WORKER_NAME and DB_WORKER_PASSWORD."
                )

        # --- video storage ---
        if self.video_out_store_service == "azure":
            if not self.video_out_store_azure_connection_string:
                raise ValueError(
                    "VIDEO_OUT_STORE_AZURE_CONNECTION_STRING is required when "
                    "VIDEO_OUT_STORE_SERVICE=azure."
                )
            if not self.video_out_store_azure_container_name:
                raise ValueError(
                    "VIDEO_OUT_STORE_AZURE_CONTAINER_NAME is required when "
                    "VIDEO_OUT_STORE_SERVICE=azure."
                )
        elif self.video_out_store_service == "aws":
            if not self.video_out_store_aws_bucket_name:
                raise ValueError(
                    "VIDEO_OUT_STORE_AWS_BUCKET_NAME is required when "
                    "VIDEO_OUT_STORE_SERVICE=aws."
                )

        return self

    # ================================================================== #
    # COMPUTED FIELDS  (derived from other fields, never read from env)
    # ================================================================== #

    @computed_field
    @property
    def video_stream_reader_url(self) -> str:
        """RTSP/RTMP URL for the drone video stream input."""
        proto    = self.video_stream_reader_protocol
        host_key = (
            f"{self.video_stream_reader_host}"
            f":{self.video_stream_reader_port}"
            f"/{self.video_stream_reader_stream_key}"
        )
        if proto in ("rtmps", "rtsps"):
            assert self.video_stream_reader_password is not None  # enforced by model_validator
            return f"{proto}://{self.video_stream_reader_username}:{self.video_stream_reader_password.get_secret_value()}@{host_key}"
        return f"{proto}://{host_key}"

    @computed_field
    @property
    def video_out_stream_url(self) -> str:
        """RTMP URL for the annotated video output stream (FFmpeg → media server)."""
        proto    = self.video_out_stream_protocol
        host_key = (
            f"{self.video_out_stream_host}"
            f":{self.video_out_stream_port}"
            f"/{self.video_out_stream_stream_key}"
        )
        if proto == "rtmps":
            assert self.video_out_stream_password is not None  # enforced by model_validator
            return f"{proto}://{self.video_out_stream_username}:{self.video_out_stream_password.get_secret_value()}@{host_key}"
        return f"{proto}://{host_key}"
