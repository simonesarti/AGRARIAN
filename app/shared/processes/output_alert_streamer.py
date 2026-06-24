import multiprocessing as mp
import multiprocessing.synchronize
from queue import Empty as QueueEmptyException
import json
import cv2
import numpy as np
from typing import Optional
import base64
from datetime import datetime as dtt
import logging
from time import time
from pydantic import BaseModel, PositiveFloat, PositiveInt, Field

from app.shared.processes.db_writer_client import DbWriterClient
from app.shared.processes.ws_server_client import WsServerClient
from app.shared.processes.messages import AnnotationSlotMetadata
from app.shared.processes.frame_buffer import FrameBuffer
from app.shared.processes.constants import (
    ALERTS_QUEUE_GET_TIMEOUT,
    ALERTS_JPEG_COMPRESSION_QUALITY,
    ALERTS_MAX_CONSECUTIVE_FAILURES,
    POISON_PILL,
)


# ================================================================

logger = logging.getLogger("main.alert_out")

if not logger.handlers:  # Avoid duplicate handlers
    _handler = logging.FileHandler('./logs/alert_out.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.WARNING)


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

    # ------- File logger --------
    log_file_path: str = "alerts.log"

    # ------- WebSocket server sidecar --------
    # URL of the ws-server sidecar HTTP API (e.g. http://ws-server:8000).
    ws_server_url: str

    # ------- Database writer sidecar --------
    # URL of the db-writer sidecar HTTP API (e.g. http://db-writer:8000).
    # The sidecar holds the privileged DB credentials; the app supplies only
    # the end-user identity (database_username / database_password).
    db_writer_url: str
    database_username: str = ""
    database_password: str = ""
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

        # Output managers — set to None here, instantiated inside run() in the child process
        self.log_file = None
        self.db_client = None
        self.ws_client = None

        self.work_finished = mp.Event()

    def _setup_managers(self):
        """Initialise file, WebSocket, and database output channels inside the child process."""

        # Initialize log file manager (required — exception propagates to run())
        self.log_file = open(self.config.log_file_path, 'a', buffering=1, encoding='utf-8')

        # Initialize DB writer client (required — exception propagates to run())
        self.db_client = DbWriterClient(self.config.db_writer_url)
        self.db_client.initialize(self.config.database_username, self.config.database_password)
        if self.config.video_stream_url:
            self.db_client.set_stream_url(self.config.video_stream_url)

        # Initialize WebSocket server client (required — exception propagates to run())
        self.ws_client = WsServerClient(self.config.ws_server_url)

    def _compress_frame(self, frame: np.ndarray) -> tuple[str, bytes]:
        """Compress frame to JPEG, returning (base64 string for WS, raw bytes for DB)."""
        compression_start = time()

        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self.config.alerts_jpeg_quality]
        _, buffer = cv2.imencode('.jpg', frame, encode_param)

        # tobytes() once; reused for both WebSocket (base64) and database (raw bytes)
        raw_bytes = buffer.tobytes()
        jpg_as_text = base64.b64encode(raw_bytes).decode('utf-8')

        logger.debug(f"Frame compressed in {(time() - compression_start) * 1000:.1f} ms")
        return jpg_as_text, raw_bytes

    def _log_alert(self, frame_id: int, alert_msg: str, timestamp: float, datetime_str: str):
        """
        Append an alert entry to the log file using the persistent file handle.

        Args:
            frame_id: Frame identifier
            alert_msg: Alert message
            timestamp: Alert timestamp
            datetime_str: ISO-formatted alert datetime
        """
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
        self._log_alert(
                frame_id=meta.frame_id,
                alert_msg=meta.alert_msg,
                timestamp=meta.timestamp,
                datetime_str=alert_datetime_str,
            )

        # Send to WebSocket server sidecar for broadcast
        self.ws_client.send_alert(alert_data)

        # Save to database via sidecar
        saved = self.db_client.save_alert(
            frame_id=meta.frame_id,
            alert_msg=meta.alert_msg,
            timestamp=meta.timestamp,
            datetime=alert_datetime,
            image_data=compressed_bytes,
            image_width=width,
            image_height=height,
        )
        if not saved:
            logger.warning(
                f"Alert for frame {meta.frame_id} was not persisted to DB "
                f"(worker unavailable or queue full)"
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

        # Close DB writer client (signals sidecar to flush and close session)
        if self.db_client:
            self.db_client.close()

        # Close WebSocket server client
        if self.ws_client:
            self.ws_client.close()

    def run(self):
        """Main process loop."""


        alert_count = 0
        consecutive_failures = 0
        poison_pill_received = False

        # Initialised to -inf so the very first alert is always dispatched regardless of cooldown.
        last_alert_timestamp = -float('inf')

        logger.info("NotificationsStreamWriter process starting.")
        logger.info(f"  WebSocket     : {self.config.ws_server_url}")
        logger.info(f"  Database      : {self.config.db_writer_url}")
        logger.info(f"  Log file      : {self.config.log_file_path}")
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

                # ---- zero-copy view of input slot ----
                logger.info("tried to read data from buffer")
                frame = self.input_frame_buffer.view(meta.slot_index)
                logger.info("slot acquired (zero-copy)")

                # ---- cooldown check and alert dispatch ----
                try:
                    if meta.alert_msg:
                        since_last = meta.timestamp - last_alert_timestamp
                        if since_last >= self.config.alerts_cooldown_s:
                            logger.info("tried to process alert")
                            self._process_alert(frame, meta)
                            logger.info("processed alert")
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
                    self.input_frame_buffer.release(meta.slot_index)

                except Exception as e:
                    self.input_frame_buffer.release(meta.slot_index)
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
