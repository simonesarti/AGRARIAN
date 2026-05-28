import multiprocessing as mp
import multiprocessing.synchronize
import logging
from queue import Empty as QueueEmptyException
from typing import Optional
import cv2
import numpy as np
from pydantic import BaseModel, PositiveFloat, PositiveInt

from src.shared.processes.video_stream_manager import VideoStreamManager
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.messages import AnnotationSlotMetadata
from src.shared.processes.constants import (
    FPS,
    CODEC,
    PIPELINE_QUEUE_TIMEOUT,
    VIDEO_WRITER_HANDOFF_TIMEOUT,
    MAX_SIZE_VIDEO_STREAM,
    VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT,
    VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT,
    VIDEO_OUT_STREAM_STARTUP_TIMEOUT,
    VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT,
    POISON_PILL,
)


# ================================================================

logger = logging.getLogger("main.video_out")

if not logger.handlers:  # Avoid duplicate handlers
    video_handler = logging.FileHandler('./logs/video_out.log', mode='w')
    video_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(video_handler)
    logger.setLevel(logging.DEBUG)

# ================================================================


class VideoProducerProcessConfig(BaseModel):
    """Configuration for VideoProducerProcess."""

    fps: PositiveInt = FPS
    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT

    # ------- Local file save --------
    video_file_path: str

    # ------- RTMP stream (media_server_url=None to disable) --------
    media_server_url: Optional[str] = None
    stream_manager_queue_max_size: PositiveInt = MAX_SIZE_VIDEO_STREAM
    stream_manager_ffmpeg_startup_timeout: PositiveFloat = VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT
    stream_manager_ffmpeg_shutdown_timeout: PositiveFloat = VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT
    stream_manager_startup_timeout: PositiveFloat = VIDEO_OUT_STREAM_STARTUP_TIMEOUT
    stream_manager_shutdown_timeout: PositiveFloat = VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT

    # ------- Persistence handoff --------
    storage_manager_handoff_timeout: PositiveFloat = VIDEO_WRITER_HANDOFF_TIMEOUT


class VideoProducerProcess(mp.Process):
    """
    Terminal process in the danger detection pipeline's video branch.

    Receives its own dedicated input from DangerAnnotationWorker (fan-out): reads
    AnnotationSlotMetadata from the input queue and the full-resolution annotated
    frame from the dedicated input FrameBuffer. Releases each slot immediately after
    reading the frame copy.

    Each frame is:
    - written to a local video file via cv2.VideoWriter
    - pushed to VideoStreamManager (background thread via FFmpeg) for RTMP streaming,
      if media_server_url is configured

    Both outputs are lazily initialised on the first frame, since cv2.VideoWriter
    and FFmpeg both require frame dimensions at construction time.

    On clean shutdown (POISON_PILL received), the local video path is passed to the
    downstream PersistenceProcess via output_queue for cloud upload. The poison pill
    is always sent to output_queue so the PersistenceProcess stops regardless.

    Termination:
    - Clean shutdown: POISON_PILL received on the input queue.
    - Error shutdown: error_event set by any process stops the loop immediately.
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: FrameBuffer,
            output_queue: mp.Queue,             # to PersistenceProcess (path + pill only)
            error_event: multiprocessing.synchronize.Event,
            config: VideoProducerProcessConfig,
    ):
        super().__init__()

        self.input_meta_queue = input_meta_queue
        self.input_frame_buffer = input_frame_buffer
        self.output_queue = output_queue
        self.error_event = error_event
        self.config = config

        self.work_finished = mp.Event()

        # Lazily initialised inside run() on the first frame
        self.writer: Optional[cv2.VideoWriter] = None
        self.stream_manager: Optional[VideoStreamManager] = None
        self._video_filename: str = ""  # set at run() start from config + datetime

    def _init_writer(self, width: int, height: int):
        fourcc = cv2.VideoWriter_fourcc(*CODEC)
        self.writer = cv2.VideoWriter(
            filename=self._video_filename,
            fourcc=fourcc,
            fps=self.config.fps,
            frameSize=(width, height),
        )
        logger.info(f"VideoWriter initialised. File: '{self._video_filename}'. Frame size: {width}×{height}")

    def _process_frame(self, frame: np.ndarray):
        height, width = frame.shape[:2]

        # Lazy-init VideoWriter on first frame (dimensions needed)
        if self.writer is None:
            self._init_writer(width=width, height=height)

        # Lazy-start VideoStreamManager on first frame (dimensions needed for FFmpeg)
        if self.stream_manager is not None and self.stream_manager.stream_thread is None:
            self.stream_manager.set_frame_dims(width=width, height=height)
            if not self.stream_manager.start():
                logger.warning("VideoStreamManager failed to start; RTMP streaming disabled.")
                self.stream_manager = None

        # Add frame to local video file
        self.writer.write(frame)

        # Push frame to Real-Time Stream Manager
        if self.stream_manager:
            self.stream_manager.push_to_queue(frame)

    def _shutdown(self):
        """Stop streaming thread, flush and close the video file, handoff to persistence."""

        if self.stream_manager:
            self.stream_manager.stop()

        save_succeeded = False
        if self.writer:
            try:
                self.writer.release()
                logger.info(f"Recording saved at '{self._video_filename}'")
                save_succeeded = True
            except Exception as e:
                logger.error(f"Error saving recording: {e}")

        # Notify PersistenceProcess: send path on success, None on failure.
        # The persistence process exits as soon as it receives this value — no pill needed.
        payload = self._video_filename if save_succeeded else None
        try:
            self.output_queue.put(payload, timeout=self.config.storage_manager_handoff_timeout)
            if save_succeeded:
                logger.info("Video path passed to persistence process.")
            else:
                logger.info("Video save failed: persistence process notified to skip upload.")
        except Exception as e:
            logger.error(f"Failed to communicate with persistence process: {e}. Setting error event.")
            self.error_event.set()

    def run(self):
        logger.info("VideoProducerProcess started.")

        poison_pill_received = False

        # Reset in case run() is ever called again (defensive)
        self.writer = None
        self.stream_manager = None

        self._video_filename = self.config.video_file_path

        if self.config.media_server_url:
            self.stream_manager = VideoStreamManager(
                mediaserver_url=self.config.media_server_url,
                fps=self.config.fps,
                queue_max_size=self.config.stream_manager_queue_max_size,
                queue_get_timeout=self.config.queue_timeout,
                ffmpeg_startup_timeout=self.config.stream_manager_ffmpeg_startup_timeout,
                ffmpeg_shutdown_timeout=self.config.stream_manager_ffmpeg_shutdown_timeout,
                startup_timeout=self.config.stream_manager_startup_timeout,
                shutdown_timeout=self.config.stream_manager_shutdown_timeout,
            )

        try:
            while not self.error_event.is_set():

                # ---- pull next frame metadata ----
                try:
                    meta = self.input_meta_queue.get(timeout=self.config.queue_timeout)
                except QueueEmptyException:
                    logger.debug("Input queue empty. Waiting for frames ...")
                    continue

                # ---- poison pill: stop ----
                if isinstance(meta, str) and meta == POISON_PILL:
                    poison_pill_received = True
                    logger.info("Poison pill received. Shutting down ...")
                    break

                assert isinstance(meta, AnnotationSlotMetadata)

                # ---- zero-copy view of input slot ----
                frame = self.input_frame_buffer.view(meta.slot_index)
                self._process_frame(frame)
                self.input_frame_buffer.release(meta.slot_index)

        except Exception as e:
            logger.critical(f"Critical error in VideoProducerProcess: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            self._shutdown()
            # detah from shared memory
            # Parent process responsible for calling unlink()
            self.input_frame_buffer.close()
            logger.info(
                "VideoProducerProcess stopped. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()
