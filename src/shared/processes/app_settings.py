import math
import re
from typing import Any, Literal, Optional

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.shared.processes.constants import (
    ALERTS_COOLDOWN_SECONDS,
    ALERTS_JPEG_COMPRESSION_QUALITY,
    ALERTS_MAX_CONSECUTIVE_FAILURES,
    ALERTS_QUEUE_GET_TIMEOUT,
    ANNOTATION_QUEUE_GET_TIMEOUT,
    ANNOTATION_QUEUE_PUT_TIMEOUT,
    AWS,
    AZURE,
    DB_HOST,
    DB_MANAGER_MAX_OVERFLOW,
    DB_MANAGER_POOL_SIZE,
    DB_MANAGER_QUEUE_SIZE,
    DB_MANAGER_QUEUE_WAIT_TIMEOUT,
    DB_MANAGER_THREAD_CLOSE_TIMEOUT,
    DB_PORT,
    DRONE_SENSOR_HEIGHT_MM,
    DRONE_SENSOR_HEIGHT_PIXELS,
    DRONE_SENSOR_WIDTH_MM,
    DRONE_SENSOR_WIDTH_PIXELS,
    DRONE_TRUE_FOCAL_LEN_MM,
    FPS,
    FRAMETELCOMB_MAX_TELEM_BUFFER_SIZE,
    FRAMETELCOMB_MAX_TIME_DIFF,
    FRAMETELCOMB_QUEUE_GET_TIMEOUT,
    FRAMETELCOMB_QUEUE_PUT_TIMEOUT,
    GOOGLE,
    LOCAL,
    MAX_SIZE_DANGER_DETECTION_RESULT,
    MAX_SIZE_DETECTION_IN,
    MAX_SIZE_FRAME_READER_OUT,
    MAX_SIZE_GEO_IN,
    MAX_SIZE_NOTIFICATIONS_STREAM,
    MAX_SIZE_SEGMENTATION_IN,
    MAX_SIZE_VIDEO_STORAGE,
    MAX_SIZE_VIDEO_STREAM,
    MODELS_QUEUE_GET_TIMEOUT,
    MODELS_QUEUE_PUT_TIMEOUT,
    MQTT,
    MQTTS,
    POISON_PILL_TIMEOUT,
    RTMP,
    RTMPS,
    RTSPS,
    SAFETY_RADIUS_M,
    SLOPE_ANGLE_THRESHOLD,
    TELEMETRY_LISTENER_HOST,
    TELEMETRY_LISTENER_MAX_INCOMING_MESSAGES,
    TELEMETRY_LISTENER_MSG_WAIT_TIMEOUT,
    TELEMETRY_LISTENER_PORT,
    TELEMETRY_LISTENER_PROTOCOL,
    TELEMETRY_LISTENER_QOS_LEVEL,
    TELEMETRY_LISTENER_RECONNECT_DELAY,
    VIDEO_OUT_STORE_DELETE_LOCAL_ON_SUCCESS,
    VIDEO_OUT_STORE_LOCAL_TARGET_DIR,
    VIDEO_OUT_STORE_MAX_UPLOAD_RETRIES,
    VIDEO_OUT_STORE_QUEUE_GET_TIMEOUT,
    VIDEO_OUT_STORE_RETRY_BACKOFF_TIME,
    VIDEO_OUT_STORE_SERVICE,
    VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,
    VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT,
    VIDEO_OUT_STREAM_HOST,
    VIDEO_OUT_STREAM_PORT,
    VIDEO_OUT_STREAM_PROTOCOL,
    VIDEO_OUT_STREAM_QUEUE_GET_TIMEOUT,
    VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,
    VIDEO_OUT_STREAM_STARTUP_TIMEOUT,
    VIDEO_OUT_STREAM_STREAM_KEY,
    VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S,
    VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES,
    VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S,
    VIDEO_STREAM_READER_FRAME_RETRY_DELAY,
    VIDEO_STREAM_READER_HOST,
    VIDEO_STREAM_READER_MAX_CONSECUTIVE_CONNECTION_FAILURES,
    VIDEO_STREAM_READER_ORIGINAL_SHAPE,
    VIDEO_STREAM_READER_PORT,
    VIDEO_STREAM_READER_PROCESSING_SHAPE,
    VIDEO_STREAM_READER_PROTOCOL,
    VIDEO_STREAM_READER_QUEUE_PUT_TIMEOUT,
    VIDEO_STREAM_READER_RECONNECT_DELAY,
    VIDEO_STREAM_READER_STREAM_KEY,
    VIDEO_WRITER_GET_FRAME_TIMEOUT,
    VIDEO_WRITER_HANDOFF_TIMEOUT,
    WEBSOCKET_HOST,
    WEBSOCKET_PORT,
    WS_MANAGER_BROADCAST_TIMEOUT,
    WS_MANAGER_PING_INTERVAL,
    WS_MANAGER_PING_TIMEOUT,
    WS_MANAGER_THREAD_CLOSE_TIMEOUT,
)


class AppSettings(BaseSettings):
    """
    Single source of truth for all pipeline configuration.

    Values are read from environment variables (case-insensitive) and from a
    .env file if present.  The field name maps 1-to-1 to the env var name:
    e.g. field `fps` reads FPS, field `db_service` reads DB_SERVICE.

    env_ignore_empty=True means an empty string in the environment is treated
    the same as "not set" and causes the field default to be used instead.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # ------------------------------------------------------------------ #
    # PIPELINE QUEUE / BUFFER SIZES
    # ------------------------------------------------------------------ #

    max_size_frame_reader_out:       int = Field(default=MAX_SIZE_FRAME_READER_OUT,       gt=0)
    max_size_detection_in:           int = Field(default=MAX_SIZE_DETECTION_IN,           gt=0)
    max_size_segmentation_in:        int = Field(default=MAX_SIZE_SEGMENTATION_IN,        gt=0)
    max_size_geo_in:                 int = Field(default=MAX_SIZE_GEO_IN,                 gt=0)
    max_size_danger_detection_result:int = Field(default=MAX_SIZE_DANGER_DETECTION_RESULT,gt=0)
    max_size_video_stream:           int = Field(default=MAX_SIZE_VIDEO_STREAM,           gt=0)
    max_size_notifications_stream:   int = Field(default=MAX_SIZE_NOTIFICATIONS_STREAM,   gt=0)
    max_size_video_storage:          int = Field(default=MAX_SIZE_VIDEO_STORAGE,          gt=0)

    # ------------------------------------------------------------------ #
    # GENERAL
    # ------------------------------------------------------------------ #

    fps: int = Field(default=FPS, gt=0)
    alerts_cooldown_seconds: float = Field(default=ALERTS_COOLDOWN_SECONDS, gt=0)
    poison_pill_timeout: float = Field(default=POISON_PILL_TIMEOUT, gt=0)

    # ------------------------------------------------------------------ #
    # DRONE HARDWARE
    # ------------------------------------------------------------------ #

    drone_true_focal_len_mm:    float = Field(default=DRONE_TRUE_FOCAL_LEN_MM,    gt=0)
    drone_sensor_width_mm:      float = Field(default=DRONE_SENSOR_WIDTH_MM,      gt=0)
    drone_sensor_height_mm:     float = Field(default=DRONE_SENSOR_HEIGHT_MM,     gt=0)
    drone_sensor_width_pixels:  int   = Field(default=DRONE_SENSOR_WIDTH_PIXELS,  gt=0)
    drone_sensor_height_pixels: int   = Field(default=DRONE_SENSOR_HEIGHT_PIXELS, gt=0)

    # ------------------------------------------------------------------ #
    # DANGER DETECTION PARAMETERS
    # ------------------------------------------------------------------ #

    safety_radius_m:          float = Field(default=SAFETY_RADIUS_M, gt=0)
    slope_angle_threshold:    float = Field(default=SLOPE_ANGLE_THRESHOLD, ge=0, le=90)
    # Parsed from "(lon1, lat1), (lon2, lat2), ..." — leave empty/unset to disable geofencing
    geofencing_vertexes: Optional[list[tuple[float, float]]] = None

    # ------------------------------------------------------------------ #
    # VIDEO STREAM READER
    # ------------------------------------------------------------------ #

    video_stream_reader_protocol:                      str           = VIDEO_STREAM_READER_PROTOCOL
    video_stream_reader_host:                          str           = VIDEO_STREAM_READER_HOST
    video_stream_reader_port:                          int           = Field(default=VIDEO_STREAM_READER_PORT, ge=1, le=65535)
    video_stream_reader_stream_key:                    str           = VIDEO_STREAM_READER_STREAM_KEY
    video_stream_reader_username:                      Optional[str] = None
    video_stream_reader_password:                      Optional[str] = None
    video_stream_reader_connection_open_timeout_s:     float         = Field(default=VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S,     gt=0)
    video_stream_reader_reconnect_delay:               float         = Field(default=VIDEO_STREAM_READER_RECONNECT_DELAY,               gt=0)
    video_stream_reader_max_consecutive_connection_failures: int     = Field(default=VIDEO_STREAM_READER_MAX_CONSECUTIVE_CONNECTION_FAILURES, ge=0)
    video_stream_reader_frame_read_timeout_s:          float         = Field(default=VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S,          gt=0)
    video_stream_reader_frame_retry_delay:             float         = Field(default=VIDEO_STREAM_READER_FRAME_RETRY_DELAY,             gt=0)
    video_stream_reader_frame_max_consecutive_failures:int           = Field(default=VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES, ge=0)
    video_stream_reader_queue_put_timeout:             float         = Field(default=VIDEO_STREAM_READER_QUEUE_PUT_TIMEOUT,             gt=0)
    # Processing resolution (frames are downscaled to this before inference)
    video_stream_reader_processing_width:  int = Field(default=VIDEO_STREAM_READER_PROCESSING_SHAPE[0], gt=0)
    video_stream_reader_processing_height: int = Field(default=VIDEO_STREAM_READER_PROCESSING_SHAPE[1], gt=0)
    # Expected original resolution — used to pre-allocate output frame buffers
    video_original_width:  int = Field(default=VIDEO_STREAM_READER_ORIGINAL_SHAPE[0], gt=0)
    video_original_height: int = Field(default=VIDEO_STREAM_READER_ORIGINAL_SHAPE[1], gt=0)

    # ------------------------------------------------------------------ #
    # TELEMETRY / MQTT
    # ------------------------------------------------------------------ #

    telemetry_listener_protocol:           str              = TELEMETRY_LISTENER_PROTOCOL
    telemetry_listener_host:               str              = TELEMETRY_LISTENER_HOST
    telemetry_listener_port:               int              = Field(default=TELEMETRY_LISTENER_PORT, ge=1, le=65535)
    telemetry_listener_username:           Optional[str]    = None
    telemetry_listener_password:           Optional[str]    = None
    telemetry_listener_qos_level:          Literal[0, 1, 2] = TELEMETRY_LISTENER_QOS_LEVEL
    telemetry_listener_reconnect_delay:    float            = Field(default=TELEMETRY_LISTENER_RECONNECT_DELAY,    gt=0)
    telemetry_listener_msg_wait_timeout:   float            = Field(default=TELEMETRY_LISTENER_MSG_WAIT_TIMEOUT,   gt=0)
    telemetry_listener_max_incoming_messages: int           = Field(default=TELEMETRY_LISTENER_MAX_INCOMING_MESSAGES, gt=0)

    # ------------------------------------------------------------------ #
    # FRAME + TELEMETRY COMBINER
    # ------------------------------------------------------------------ #

    frametelcomb_max_telem_buffer_size: int   = Field(default=FRAMETELCOMB_MAX_TELEM_BUFFER_SIZE, gt=0)
    frametelcomb_max_time_diff:         float = Field(default=FRAMETELCOMB_MAX_TIME_DIFF,         ge=0)
    frametelcomb_queue_get_timeout:     float = Field(default=FRAMETELCOMB_QUEUE_GET_TIMEOUT,     gt=0)
    frametelcomb_queue_put_timeout:     float = Field(default=FRAMETELCOMB_QUEUE_PUT_TIMEOUT,     gt=0)

    # ------------------------------------------------------------------ #
    # MODEL WORKERS (detection, segmentation, geo)
    # ------------------------------------------------------------------ #

    models_queue_get_timeout: float = Field(default=MODELS_QUEUE_GET_TIMEOUT, gt=0)
    models_queue_put_timeout: float = Field(default=MODELS_QUEUE_PUT_TIMEOUT, gt=0)

    # ------------------------------------------------------------------ #
    # DANGER ANNOTATION
    # ------------------------------------------------------------------ #

    annotation_queue_get_timeout: float = Field(default=ANNOTATION_QUEUE_GET_TIMEOUT, gt=0)
    annotation_queue_put_timeout: float = Field(default=ANNOTATION_QUEUE_PUT_TIMEOUT, gt=0)

    # ------------------------------------------------------------------ #
    # ALERTS WRITER
    # ------------------------------------------------------------------ #

    alerts_queue_get_timeout:          float = Field(default=ALERTS_QUEUE_GET_TIMEOUT,          gt=0)
    alerts_max_consecutive_failures:   int   = Field(default=ALERTS_MAX_CONSECUTIVE_FAILURES,   ge=0)
    alerts_jpeg_compression_quality:   int   = Field(default=ALERTS_JPEG_COMPRESSION_QUALITY,   ge=0, le=100)

    # ------------------------------------------------------------------ #
    # WEBSOCKET SERVER
    # ------------------------------------------------------------------ #

    websocket_host: str = WEBSOCKET_HOST
    websocket_port: int = Field(default=WEBSOCKET_PORT, ge=1, le=65535)
    ws_manager_broadcast_timeout:    float = Field(default=WS_MANAGER_BROADCAST_TIMEOUT,    gt=0)
    ws_manager_ping_interval:        float = Field(default=WS_MANAGER_PING_INTERVAL,        gt=0)
    ws_manager_ping_timeout:         float = Field(default=WS_MANAGER_PING_TIMEOUT,         gt=0)
    ws_manager_thread_close_timeout: float = Field(default=WS_MANAGER_THREAD_CLOSE_TIMEOUT, gt=0)

    # ------------------------------------------------------------------ #
    # DATABASE
    # ------------------------------------------------------------------ #

    # Supported: postgresql, mysql, sqlite — leave empty/unset to disable
    db_service:                  Optional[str] = None
    db_host:                     str           = DB_HOST
    db_port:                     int           = Field(default=DB_PORT, ge=1, le=65535)
    db_worker_name:              Optional[str] = None   # DB connection role
    db_worker_password:          Optional[str] = None
    db_username:                 str           = ""     # app-level auth (users table)
    db_password:                 str           = ""
    db_manager_pool_size:        int           = Field(default=DB_MANAGER_POOL_SIZE,        gt=0)
    db_manager_max_overflow:     int           = Field(default=DB_MANAGER_MAX_OVERFLOW,     ge=-1)
    db_manager_queue_size:       int           = Field(default=DB_MANAGER_QUEUE_SIZE,       gt=0)
    db_manager_queue_wait_timeout:   float     = Field(default=DB_MANAGER_QUEUE_WAIT_TIMEOUT,   gt=0)
    db_manager_thread_close_timeout: float     = Field(default=DB_MANAGER_THREAD_CLOSE_TIMEOUT, gt=0)

    # ------------------------------------------------------------------ #
    # VIDEO WRITER
    # ------------------------------------------------------------------ #

    video_writer_get_frame_timeout: float = Field(default=VIDEO_WRITER_GET_FRAME_TIMEOUT, gt=0)
    video_writer_handoff_timeout:   float = Field(default=VIDEO_WRITER_HANDOFF_TIMEOUT,   gt=0)

    # ------------------------------------------------------------------ #
    # VIDEO STREAM OUTPUT (RTMP → media server)
    # ------------------------------------------------------------------ #

    video_out_stream_protocol:               str           = VIDEO_OUT_STREAM_PROTOCOL
    video_out_stream_host:                   str           = VIDEO_OUT_STREAM_HOST
    video_out_stream_port:                   int           = Field(default=VIDEO_OUT_STREAM_PORT, ge=1, le=65535)
    video_out_stream_stream_key:             str           = VIDEO_OUT_STREAM_STREAM_KEY
    video_out_stream_username:               Optional[str] = None
    video_out_stream_password:               Optional[str] = None
    video_out_stream_queue_get_timeout:      float         = Field(default=VIDEO_OUT_STREAM_QUEUE_GET_TIMEOUT,      gt=0)
    video_out_stream_ffmpeg_startup_timeout: float         = Field(default=VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT, gt=0)
    video_out_stream_ffmpeg_shutdown_timeout:float         = Field(default=VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,gt=0)
    video_out_stream_startup_timeout:        float         = Field(default=VIDEO_OUT_STREAM_STARTUP_TIMEOUT,        gt=0)
    video_out_stream_shutdown_timeout:       float         = Field(default=VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,       gt=0)

    # ------------------------------------------------------------------ #
    # VIDEO STORAGE (cloud / local upload after recording)
    # ------------------------------------------------------------------ #

    # Service: azure, aws, local  (google not yet implemented)
    video_out_store_service:              str  = VIDEO_OUT_STORE_SERVICE
    video_out_store_delete_local_on_success: bool  = VIDEO_OUT_STORE_DELETE_LOCAL_ON_SUCCESS
    video_out_store_queue_get_timeout:    float = Field(default=VIDEO_OUT_STORE_QUEUE_GET_TIMEOUT,    gt=0)
    video_out_store_max_upload_retries:   int   = Field(default=VIDEO_OUT_STORE_MAX_UPLOAD_RETRIES,   ge=0)
    video_out_store_retry_backoff_time:   float = Field(default=VIDEO_OUT_STORE_RETRY_BACKOFF_TIME,   gt=0)
    # Azure Blob Storage
    video_out_store_azure_connection_string: Optional[str] = None
    video_out_store_azure_container_name:    Optional[str] = None
    video_out_store_azure_blob_prefix:       str           = ""
    # AWS S3
    video_out_store_aws_bucket_name:         Optional[str] = None
    video_out_store_aws_key_prefix:          str           = ""
    video_out_store_aws_access_key_id:       Optional[str] = None
    video_out_store_aws_secret_access_key:   Optional[str] = None
    video_out_store_aws_region_name:         Optional[str] = None
    # Local (testing / no-cloud fallback)
    video_out_store_local_target_dir: str = VIDEO_OUT_STORE_LOCAL_TARGET_DIR

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

    @field_validator("db_service", mode="before")
    @classmethod
    def _normalize_db_service(cls, v: Any) -> Optional[str]:
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "none", "not_specified")):
            return None
        return v.strip().lower()

    @field_validator("geofencing_vertexes", mode="before")
    @classmethod
    def _parse_geofencing(cls, v: Any) -> Optional[list[tuple[float, float]]]:
        if v is None:
            return None
        s = str(v).strip()
        if s.lower() in ("", "none", "not_specified"):
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
        allowed_in = (RTSP, RTMP, RTMPS, RTSPS)
        if self.video_stream_reader_protocol not in allowed_in:
            raise ValueError(
                f"VIDEO_STREAM_READER_PROTOCOL must be one of {allowed_in}, "
                f"got '{self.video_stream_reader_protocol}'."
            )
        if self.video_stream_reader_protocol in (RTMPS, RTSPS):
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
        allowed_mqtt = (MQTT, MQTTS)
        if self.telemetry_listener_protocol not in allowed_mqtt:
            raise ValueError(
                f"TELEMETRY_LISTENER_PROTOCOL must be one of {allowed_mqtt}, "
                f"got '{self.telemetry_listener_protocol}'."
            )
        if self.telemetry_listener_protocol == MQTTS:
            if not (self.telemetry_listener_username and self.telemetry_listener_password):
                raise ValueError(
                    "MQTTS requires TELEMETRY_LISTENER_USERNAME and TELEMETRY_LISTENER_PASSWORD."
                )
        else:
            self.telemetry_listener_username = None
            self.telemetry_listener_password = None

        # --- video stream output ---
        allowed_out = (RTMP, RTMPS)
        if self.video_out_stream_protocol not in allowed_out:
            raise ValueError(
                f"VIDEO_OUT_STREAM_PROTOCOL must be one of {allowed_out}, "
                f"got '{self.video_out_stream_protocol}'."
            )
        if self.video_out_stream_protocol == RTMPS:
            if not (self.video_out_stream_username and self.video_out_stream_password):
                raise ValueError(
                    "RTMPS requires VIDEO_OUT_STREAM_USERNAME and VIDEO_OUT_STREAM_PASSWORD."
                )
        else:
            self.video_out_stream_username = None
            self.video_out_stream_password = None

        # --- database ---
        allowed_db = (None, "sqlite", "postgresql", "mysql")
        if self.db_service not in allowed_db:
            raise ValueError(
                f"DB_SERVICE must be one of {allowed_db}, got '{self.db_service}'."
            )

        # --- video storage ---
        allowed_store = (AZURE, AWS, LOCAL)
        if self.video_out_store_service not in allowed_store:
            if self.video_out_store_service == GOOGLE:
                raise NotImplementedError("Google Cloud Storage is not yet implemented.")
            raise ValueError(
                f"VIDEO_OUT_STORE_SERVICE must be one of {allowed_store}, "
                f"got '{self.video_out_store_service}'."
            )
        if self.video_out_store_service == AZURE:
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
        elif self.video_out_store_service == AWS:
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
        if proto in (RTMPS, RTSPS):
            return f"{proto}://{self.video_stream_reader_username}:{self.video_stream_reader_password}@{host_key}"
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
        if proto == RTMPS:
            return f"{proto}://{self.video_out_stream_username}:{self.video_out_stream_password}@{host_key}"
        return f"{proto}://{host_key}"
