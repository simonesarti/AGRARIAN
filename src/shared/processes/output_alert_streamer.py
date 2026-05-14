import multiprocessing as mp
import multiprocessing.synchronize
from queue import Empty as QueueEmptyException
import json
import cv2
import numpy as np
from typing import Literal, Optional
import base64
from datetime import datetime as dtt
import logging
from time import time
from pydantic import BaseModel, PositiveFloat, PositiveInt, Field

from src.shared.processes.db_manager import DatabaseManager
from src.shared.processes.websocket_manager import WebSocketManager
from src.shared.processes.messages import AnnotationSlotMetadata
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.constants import (
    ALERTS_QUEUE_GET_TIMEOUT,
    ALERTS_JPEG_COMPRESSION_QUALITY,
    ALERTS_MAX_CONSECUTIVE_FAILURES,
    WSS_PORT,
    WS_MANAGER_PING_INTERVAL,
    WS_MANAGER_PING_TIMEOUT,
    WS_MANAGER_BROADCAST_TIMEOUT,
    WS_MANAGER_THREAD_CLOSE_TIMEOUT,
    DB_PORT,
    DB_MANAGER_POOL_SIZE,
    DB_MANAGER_MAX_OVERFLOW,
    DB_MANAGER_QUEUE_WAIT_TIMEOUT,
    DB_MANAGER_THREAD_CLOSE_TIMEOUT,
    DB_MANAGER_QUEUE_SIZE,
    DB_NAME,
    POISON_PILL,
)


# ================================================================

logger = logging.getLogger("main.alert_out")

if not logger.handlers:  # Avoid duplicate handlers
    _handler = logging.FileHandler('./logs/alert_out.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)


# ================================================================


class NotificationsStreamWriterConfig(BaseModel):
    """Configuration for NotificationsStreamWriter."""

    # Alert filtering
    # Minimum seconds that must pass between two consecutive alert dispatches.
    alerts_cooldown_s: PositiveFloat
    alerts_jpeg_quality: int = Field(default=ALERTS_JPEG_COMPRESSION_QUALITY, ge=0, le=100)
    alerts_max_consecutive_failures: PositiveInt = ALERTS_MAX_CONSECUTIVE_FAILURES

    # Queue timeouts
    queue_get_timeout: PositiveFloat = ALERTS_QUEUE_GET_TIMEOUT

    # ------- File logger (log_file_path=None to disable) --------
    log_file_path: Optional[str] = "alerts.log"

    # ------- WebSocket server (websocket_host=None to disable) --------
    websocket_host: Optional[str] = None
    websocket_port: int = Field(default=WSS_PORT, ge=1, le=65535)
    ws_ping_interval: PositiveFloat = WS_MANAGER_PING_INTERVAL
    ws_ping_timeout: PositiveFloat = WS_MANAGER_PING_TIMEOUT
    ws_broadcast_timeout: PositiveFloat = WS_MANAGER_BROADCAST_TIMEOUT
    ws_thread_close_timeout: PositiveFloat = WS_MANAGER_THREAD_CLOSE_TIMEOUT

    # ------- Database connection (database_service=None to disable) --------
    # Supported services: "postgresql", "mysql", "sqlite".
    # For postgresql / mysql: database_host, database_port, database_worker_name,
    #   and database_worker_password are used to build the SQLAlchemy connection URL.
    # For sqlite: connection params are ignored; the DB file is named DB_NAME.
    # database_username / database_password are app-level credentials checked against
    #   the users table (distinct from the DB role credentials above).
    database_service: Optional[Literal["postgresql", "mysql", "sqlite"]] = None
    database_host: Optional[str] = None
    database_port: int = Field(default=DB_PORT, ge=1, le=65535)
    database_worker_name: Optional[str] = None    # DB role (connection credential)
    database_worker_password: Optional[str] = None
    database_username: str = ""                   # app-level auth (users table)
    database_password: str = ""
    db_pool_size: PositiveInt = DB_MANAGER_POOL_SIZE
    db_max_overflow: int = Field(default=DB_MANAGER_MAX_OVERFLOW, ge=-1)
    db_queue_get_timeout: PositiveFloat = DB_MANAGER_QUEUE_WAIT_TIMEOUT
    db_thread_close_timeout: PositiveFloat = DB_MANAGER_THREAD_CLOSE_TIMEOUT
    db_alerts_queue_size: PositiveInt = DB_MANAGER_QUEUE_SIZE
    # Video stream URL written to the flights table so the UI can fetch it from the DB.
    # Should match the media_server_url passed to VideoProducerProcess.
    video_stream_url: Optional[str] = None



class NotificationsStreamWriter(mp.Process):
    """
    Alert notification process in the danger detection pipeline.

    Receives its own dedicated input from DangerAnnotationWorker (fan-out): it owns
    the input_frame_buffer slot lifecycle and releases each slot immediately after
    reading the frame copy.

    Reads AnnotationSlotMetadata from the upstream queue and the corresponding
    full-resolution annotated frame from the shared FrameBuffer. Applies a cooldown
    filter and, when an alert should be dispatched, compresses the frame as JPEG and
    delivers it via any enabled combination of: log file, WebSocket broadcast, and
    SQL database.

    If video_stream_url is set in config and the database is enabled, the URL is
    written to the current flight record once at startup so the UI can retrieve it.

    At least one output channel (file, WebSocket, database) must be successfully
    initialised at startup; if none can be started the error_event is set and the
    process shuts down.

    Termination:
    - Clean shutdown: POISON_PILL received on the input queue stops the loop.
    - Error shutdown: if error_event is set by any process, the loop stops
      immediately without flushing.
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: FrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: NotificationsStreamWriterConfig,
    ):
        super().__init__()

        self.input_meta_queue = input_meta_queue
        self.input_frame_buffer = input_frame_buffer
        self.error_event = error_event
        self.config = config

        # Build DB URL from config fields; None means database output is disabled.
        self.database_url = self._build_database_url()

        # Output managers — set to None here, instantiated inside run() in the child process
        self.log_file = None
        self.db_manager = None
        self.ws_manager = None

        self.work_finished = mp.Event()

    def _build_database_url(self) -> Optional[str]:
        """Construct SQLAlchemy database URL from config fields. Returns None if disabled."""
        if not self.config.database_service:
            return None
        if not (self.config.database_worker_name and self.config.database_worker_password):
            logger.warning("DB worker credentials not provided; database output disabled.")
            return None
        auth = f"{self.config.database_worker_name}:{self.config.database_worker_password}@"
        addr = f"{self.config.database_host}:{self.config.database_port}"
        if self.config.database_service == "postgresql":
            return f"postgresql://{auth}{addr}/{DB_NAME}"
        elif self.config.database_service == "mysql":
            return f"mysql+pymysql://{auth}{addr}/{DB_NAME}"
        elif self.config.database_service == "sqlite":
            return f"sqlite:///{DB_NAME}"
        else:
            logger.warning(f"Unknown database_service '{self.config.database_service}'; database output disabled.")
            return None

    def _setup_managers(self):
        """Initialise file, WebSocket, and database output channels inside the child process."""

        # Initialize log file manager
        try:
            if self.config.log_file_path:
                self.log_file = open(self.config.log_file_path, 'a', buffering=1, encoding='utf-8')
        except Exception as e:
            self.log_file = None    # ensure log_file stays None
            logger.error(f"Failed to open log file '{self.config.log_file_path}': {e}. Continuing without ...")

        # Initialize DB manager
        try:
            if self.database_url:
                self.db_manager = DatabaseManager(
                    database_url=self.database_url,
                    alerts_queue_size=self.config.db_alerts_queue_size,
                    pool_size=self.config.db_pool_size,
                    max_overflow=self.config.db_max_overflow,
                    queue_get_timeout=self.config.db_queue_get_timeout,
                    thread_close_timeout=self.config.db_thread_close_timeout,
                )
                self.db_manager.initialize(self.config.database_username, self.config.database_password)
                if self.config.video_stream_url:
                    self.db_manager.set_stream_url(self.config.video_stream_url)
        except Exception as e:
            self.db_manager = None  # ensure db_manager stays None
            logger.error(f"Failed to initialise database manager: {e}. Continuing without ...")

        # Initialize WebSocket manager
        try:
            if self.config.websocket_host:
                self.ws_manager = WebSocketManager(
                    host=self.config.websocket_host,
                    port=self.config.websocket_port,
                    ping_interval=self.config.ws_ping_interval,
                    ping_timeout=self.config.ws_ping_timeout,
                    broadcast_timeout=self.config.ws_broadcast_timeout,
                    thread_close_timeout=self.config.ws_thread_close_timeout,
                )
                self.ws_manager.start()
        except Exception as e:
            self.ws_manager = None  # ensure ws_manager stays None
            logger.error(f"Failed to initialise WebSocket server: {e}. Continuing without ...")

        # At least one output channel must be available
        if not (self.db_manager or self.ws_manager or self.log_file):
            self.error_event.set()
            logger.error(
                "Error event set: "
                "no output channel (file, WebSocket, database) could be initialised. "
                "Shutting down the application ..."
            )
            raise RuntimeError("No output managers available")

    def _compress_frame(self, frame: np.ndarray) -> tuple[Optional[str], Optional[bytes]]:
        """
        Compress frame to JPEG.

        Returns:
            (base64_encoded_string, raw_bytes): base64 string for WebSocket transmission,
            raw bytes for database storage. Either value is None if the corresponding
            output channel is not active.
            Returns (None, None) if neither WebSocket nor database is active.
        """
        if not (self.ws_manager or self.db_manager):
            logger.debug("No WS nor DB active; skipping frame compression.")
            return None, None

        compression_start = time()

        # Encode as JPEG
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self.config.alerts_jpeg_quality]
        _, buffer = cv2.imencode('.jpg', frame, encode_param)

        # tobytes() once; reused for both WebSocket (base64) and database (raw bytes)
        raw_bytes = buffer.tobytes()

        # Convert to base64 for WebSocket transmission
        jpg_as_text = base64.b64encode(raw_bytes).decode('utf-8') if self.ws_manager else None

        # Get raw bytes for database storage
        compressed_bytes = raw_bytes if self.db_manager else None

        logger.debug(f"Frame compressed in {(time() - compression_start) * 1000:.1f} ms")
        return jpg_as_text, compressed_bytes

    def _log_alert(self, frame_id: int, alert_msg: str, timestamp: float, datetime_str: str):
        """
        Append an alert entry to the log file using the persistent file handle.

        Args:
            frame_id: Frame identifier
            alert_msg: Alert message
            timestamp: Alert timestamp
            datetime_str: ISO-formatted alert datetime
        """
        if not self.log_file:
            return
        try:
            log_entry = {
                'frame_id': frame_id,
                'alert_msg': alert_msg,
                'timestamp': timestamp,
                'datetime': datetime_str,
            }
            # Writing to a line-buffered handle is highly efficient
            self.log_file.write(json.dumps(log_entry) + '\n')
            logger.debug(f"Alert logged to file: frame_id={frame_id}")
        except Exception as e:
            logger.error(f"Error writing alert to log file: {e}")

    def _process_alert(self, frame: np.ndarray, meta: AnnotationSlotMetadata):
        """
        Process a confirmed alert: compress, log, broadcast via WebSocket, and persist to DB.

        Args:
            frame: Full-resolution annotated BGR frame read from shared memory.
            meta: Metadata carrying frame_id, timestamp, and alert_msg.
        """
        logger.info(f"Processing alert: frame_id={meta.frame_id}, msg='{meta.alert_msg}'")

        # Compress frame (results are None if the corresponding manager is inactive)
        jpg_as_text, compressed_bytes = self._compress_frame(frame)

        # Create alert data structure
        alert_datetime = dtt.fromtimestamp(meta.timestamp)
        alert_datetime_str = alert_datetime.isoformat()
        height, width = frame.shape[:2]

        alert_data = {
            'frame_id': meta.frame_id,
            'alert_msg': meta.alert_msg,
            'timestamp': meta.timestamp,
            'datetime': alert_datetime_str,
            'image': jpg_as_text,
            'width': width,
            'height': height,
            'compression': 'jpeg',
        }

        # Log alert to file using the persistent handle
        if self.log_file:
            self._log_alert(
                frame_id=meta.frame_id,
                alert_msg=meta.alert_msg,
                timestamp=meta.timestamp,
                datetime_str=alert_datetime_str,
            )

        # Queue for WebSocket broadcast
        if self.ws_manager:
            self.ws_manager.queue_alert(alert_data)

        # Save to database
        if self.db_manager:
            self.db_manager.save_alert(
                frame_id=meta.frame_id,
                alert_msg=meta.alert_msg,
                timestamp=meta.timestamp,
                datetime=alert_datetime,
                image_data=compressed_bytes,
                image_width=width,
                image_height=height,
            )

    def _cleanup(self):
        """Close all output managers."""
        

        # Close log file
        if self.log_file:
            try:
                self.log_file.close()
                logger.info("Alert log file closed.")
            except Exception as e:
                logger.error(f"Failed to close alert log file: {e}")

        # Close database (handles errors internally)
        if self.db_manager:
            self.db_manager.close()

        # Stop WebSocket server (handles errors internally)
        if self.ws_manager:
            self.ws_manager.stop()

    def run(self):
        """Main process loop."""

        alert_count = 0
        consecutive_failures = 0
        poison_pill_received = False

        # Initialised to -inf so the very first alert is always dispatched regardless of cooldown.
        last_alert_timestamp = -float('inf')

        ws_status = f"ws://{self.config.websocket_host}:{self.config.websocket_port}" if self.config.websocket_host else "disabled"
        db_status = self.database_url if self.database_url else "disabled"
        logfile_status = self.config.log_file_path if self.config.log_file_path else "disabled"

        logger.info("NotificationsStreamWriter process starting.")
        logger.info(f"  WebSocket     : {ws_status}")
        logger.info(f"  Database      : {db_status}")
        logger.info(f"  Log file      : {logfile_status}")
        logger.info(f"  JPEG quality  : {self.config.alerts_jpeg_quality}")
        logger.info(f"  Alert cooldown: {self.config.alerts_cooldown_s} s")

        try:

            # Instantiate output managers inside run() so connections are established
            # in the child process, not inherited from the parent.
            self._setup_managers()

            # ---------------------------------
            # Frame processing loop
            # ---------------------------------

            while not self.error_event.is_set():

                # ---- pull next frame metadata ----
                try:
                    meta = self.input_meta_queue.get(timeout=self.config.queue_get_timeout)
                except QueueEmptyException:
                    logger.debug("Input queue empty. Waiting for next frame ...")
                    continue

                # ---- poison pill: stop ----
                if isinstance(meta, str) and meta == POISON_PILL:
                    poison_pill_received = True
                    logger.info("Found sentinel value on queue. Stopping.")
                    break

                assert isinstance(meta, AnnotationSlotMetadata)

                # ---- read frame from shared memory and release slot immediately ----
                # read() returns a copy, so the slot can be freed right away.
                frame = self.input_frame_buffer.read(meta.slot_index)
                self.input_frame_buffer.release(meta.slot_index)

                # ---- cooldown check and alert dispatch ----
                try:
                    if meta.alert_msg:
                        since_last = meta.timestamp - last_alert_timestamp
                        if since_last >= self.config.alerts_cooldown_s:
                            self._process_alert(frame, meta)
                            last_alert_timestamp = meta.timestamp
                            alert_count += 1
                            logger.debug(
                                f"Frame {meta.frame_id}: alert dispatched. "
                                f"Msg: '{meta.alert_msg}'."
                            )
                        else:
                            logger.debug(
                                f"Frame {meta.frame_id}: alert '{meta.alert_msg}' suppressed by cooldown "
                                f"({since_last:.1f}s elapsed, {self.config.alerts_cooldown_s}s required)."
                            )

                    # reset consecutive failure counter on any successful pass through this frame
                    consecutive_failures = 0

                except Exception as e:
                    consecutive_failures += 1
                    if consecutive_failures < self.config.alerts_max_consecutive_failures:
                        logger.warning(
                            f"Error processing alert for frame {meta.frame_id}: {e}. "
                            f"Consecutive failures: {consecutive_failures} "
                            f"(max {self.config.alerts_max_consecutive_failures}). "
                            "Continuing ...", exc_info=True
                        )
                    else:
                        logger.error(
                            "Error event set: "
                            "threshold for maximum consecutive alert processing failures reached. "
                            "Shutting down ..."
                        )
                        self.error_event.set()
                        break

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in notifications streamer process: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            # Final cleanup
            self._cleanup()
            # Detach from shared memory in this process.
            # The parent is responsible for calling unlink() after all processes have finished.
            self.input_frame_buffer.close()
            
            logger.info(
                "NotificationsStreamWriter process stopped. "
                f"Total alerts dispatched: {alert_count}."
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()
