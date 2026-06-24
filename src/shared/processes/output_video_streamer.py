import multiprocessing as mp
import multiprocessing.synchronize
import threading
import logging
from queue import Empty as QueueEmptyException, Full as QueueFullException
from time import time
from typing import Optional
import cv2
import numpy as np
from pydantic import BaseModel, PositiveFloat, PositiveInt

from src.shared.processes.video_stream_manager import VideoStreamManager, VideoFileWriter
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.messages import AnnotationSlotMetadata
from src.shared.processes.constants import (
    FPS,
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
    logger.setLevel(logging.WARNING)

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
    frame from the dedicated input FrameBuffer.

    The main loop copies each frame out of its SHM slot, releases the slot, and fans
    the frame out to two independent FFmpeg sinks. Each sink owns a background thread
    and bounded queue, so the slow encode/write happens off the main loop and a stall
    in one sink never blocks the other:

    - VideoFileWriter: GPU-encoded (h264_nvenc) local mp4 recording. Connection-
      independent and never reconnects. Encoding on the GPU's dedicated encoder ASIC
      keeps the recording at full frame rate even when the CPU is saturated by the
      rest of the pipeline (the previous in-process cv2.VideoWriter software encode
      was CPU-starved and dropped ~half the frames under load).
    - VideoStreamManager: libx264 RTMP stream to the media server (only if
      media_server_url is configured). Reconnects automatically if the link drops.

    Both sinks are lazily started on the first frame, since FFmpeg needs the frame
    dimensions at launch. On clean shutdown (POISON_PILL) each sink drains its queue
    and closes FFmpeg so the encoded outputs are finalized; the local video path is
    then passed to the downstream PersistenceProcess via output_queue for upload.

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

        # Lazily started on the first frame (FFmpeg needs frame dims at launch).
        self.file_writer: Optional[VideoFileWriter] = None
        self.stream_manager: Optional[VideoStreamManager] = None
        self._video_filename: str = ""      # set at run() start from config
        self._recording_active = False      # True once the file sink starts cleanly

    def _start_sinks(self, width: int, height: int):
        """Start both FFmpeg sinks on the first frame, once dimensions are known."""
        if self.file_writer is not None:
            self.file_writer.set_frame_dims(width, height)
            if self.file_writer.start():
                self._recording_active = True
                logger.info(f"Recording started: '{self._video_filename}' ({width}×{height})")
            else:
                logger.error("VideoFileWriter failed to start; local recording disabled.")
                self.file_writer = None

        if self.stream_manager is not None:
            self.stream_manager.set_frame_dims(width, height)
            if not self.stream_manager.start():
                logger.warning("VideoStreamManager failed to start; RTMP streaming disabled.")
                self.stream_manager = None

    def _shutdown(self):
        """Stop both sinks (drain + finalize), then hand the recording off to persistence."""

        # Finalize the recording first so the mp4 is safe even if stream cleanup is slow.
        save_succeeded = False
        if self.file_writer is not None:
            self.file_writer.stop()
            save_succeeded = self._recording_active
            if save_succeeded:
                logger.info(f"Recording saved at '{self._video_filename}'")

        # Stop the RTMP stream manager (can be slow; local file is already safe).
        if self.stream_manager is not None:
            self.stream_manager.stop()

        # Notify PersistenceProcess: send path on success, None on failure/absence.
        # The persistence process exits as soon as it receives this value — no pill needed.
        payload = self._video_filename if save_succeeded else None
        try:
            self.output_queue.put(payload, timeout=self.config.storage_manager_handoff_timeout)
            if save_succeeded:
                logger.info("Video path passed to persistence process.")
            else:
                logger.info("Video save failed/absent: persistence process notified to skip upload.")
        except Exception as e:
            logger.error(f"Failed to communicate with persistence process: {e}. Setting error event.")
            self.error_event.set()

    def run(self):

        logger.info("VideoProducerProcess started.")

        poison_pill_received = False

        # Reset in case run() is ever called again (defensive)
        self.file_writer = None
        self.stream_manager = None
        self._recording_active = False
        self._video_filename = self.config.video_file_path

        # Local GPU-encoded recording sink (always present).
        self.file_writer = VideoFileWriter(
            file_path=self._video_filename,
            fps=self.config.fps,
            queue_max_size=self.config.stream_manager_queue_max_size,
            queue_get_timeout=self.config.queue_timeout,
            ffmpeg_startup_timeout=self.config.stream_manager_ffmpeg_startup_timeout,
            ffmpeg_shutdown_timeout=self.config.stream_manager_ffmpeg_shutdown_timeout,
            startup_timeout=self.config.stream_manager_startup_timeout,
            shutdown_timeout=self.config.stream_manager_shutdown_timeout,
        )

        # Optional RTMP streaming sink.
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

        sinks_started = False

        # ---- diagnostics: is the loop input-starved (supply-bound) or backlogged
        #      (VideoProducer-bound). Summarised once per second at INFO. ----
        _diag_frames = 0      # frames processed this window
        _diag_empty = 0       # empty gets this window (loop idle waiting for input)
        _diag_qmax = 0        # max input backlog seen after a get
        _diag_proc_sum = 0.0  # sum of per-frame processing time (read+release+push)
        _diag_proc_max = 0.0

        try:
            while not self.error_event.is_set():

                # ---- pull next frame metadata ----
                try:
                    meta = self.input_meta_queue.get(timeout=self.config.queue_timeout)
                except QueueEmptyException:
                    _diag_empty += 1
                    continue

                # ---- poison pill: stop ----
                if isinstance(meta, str) and meta == POISON_PILL:
                    poison_pill_received = True
                    logger.info("Poison pill received. Shutting down ...")
                    break

                assert isinstance(meta, AnnotationSlotMetadata)

                _t_proc = time()
                _qd = self.input_meta_queue.qsize()  # frames still queued behind this one

                # Copy frame out of SHM and release the slot immediately so the
                # upstream annotator can reuse it regardless of sink queue depth.
                frame = self.input_frame_buffer.read(meta.slot_index)
                self.input_frame_buffer.release(meta.slot_index)

                # Lazy-start both sinks on the first frame (dims known only at runtime).
                if not sinks_started:
                    h, w = frame.shape[:2]
                    self._start_sinks(w, h)
                    sinks_started = True

                # Fan out: each sink encodes/writes on its own thread (non-blocking).
                if self.file_writer is not None:
                    self.file_writer.push_to_queue(frame)
                if self.stream_manager is not None:
                    self.stream_manager.push_to_queue(frame)

                # ---- diagnostics summary (once per ~second) ----
                _proc = time() - _t_proc
                _diag_frames += 1
                _diag_proc_sum += _proc
                if _proc > _diag_proc_max:
                    _diag_proc_max = _proc
                if _qd > _diag_qmax:
                    _diag_qmax = _qd
                if _diag_frames >= self.config.fps:
                    logger.info(
                        f"[DIAG main] frames/s={_diag_frames} | empty_gets={_diag_empty} | "
                        f"in_backlog_max={_diag_qmax} | "
                        f"proc_avg={_diag_proc_sum / _diag_frames * 1000:.1f}ms | "
                        f"proc_max={_diag_proc_max * 1000:.1f}ms"
                    )
                    _diag_frames = _diag_empty = _diag_qmax = 0
                    _diag_proc_sum = _diag_proc_max = 0.0

        except Exception as e:
            logger.critical(f"Critical error in VideoProducerProcess: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            self._shutdown()
            # Parent process responsible for calling unlink()
            self.input_frame_buffer.close()
            logger.info(
                "VideoProducerProcess stopped. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()


if __name__ == "__main__":

    from time import perf_counter, sleep
    from pathlib import Path

    from src.shared.processes.messages import AnnotationSlotMetadata
    from src.shared.processes.constants import POISON_PILL

    # ---- config ----
    # Frame shape must match what AnnotationWorker produces (original output resolution).
    FRAME_SHAPE   = (1080, 1920, 3)   # (H, W, C)
    N_SLOTS       = 3
    PRODUCER_FPS  = 30                # simulated upstream cadence
    DURATION_S    = 30                # how long to run

    # Set to an RTMP URL to benchmark with streaming, or None to test disk-write only.
    RTMP_URL = "rtmp://172.17.0.2:1935/annot"  # e.g. "rtmp://localhost:1935/live/bench"

    # Set one to True to test the corresponding shutdown path.
    TRIGGER_POISON_PILL = True   # clean shutdown after DURATION_S
    TRIGGER_ERROR       = False  # force error_event instead

    # ---- logging ----
    Path("./logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )
    # Surface the per-sink [TIMING ...] debug lines (the module loggers are pinned to
    # WARNING by default so they stay quiet under the full pipeline).
    for _ln in ("main.video_out", "main.video_out.stream", "main.video_out.file"):
        logging.getLogger(_ln).setLevel(logging.DEBUG)

    # ---- shared objects ----
    error_event   = mp.Event()
    input_meta_q  = mp.Queue(maxsize=N_SLOTS)
    input_buf     = FrameBuffer(frame_shape=FRAME_SHAPE, n_slots=N_SLOTS)
    output_q      = mp.Queue(maxsize=1)

    config = VideoProducerProcessConfig(
        fps=PRODUCER_FPS,
        video_file_path="./logs/bench_output.mp4",
        media_server_url=RTMP_URL,
    )

    process = VideoProducerProcess(
        input_meta_queue=input_meta_q,
        input_frame_buffer=input_buf,
        output_queue=output_q,
        error_event=error_event,
        config=config,
    )

    # ---- counters (written only from producer thread) ----
    stats = {"produced": 0, "dropped_no_slot": 0, "dropped_queue_full": 0}

    # Pre-build a base frame that resembles a real annotated output:
    # uniform background + text overlay so each frame is slightly different
    # and the codec does real work (a fully static frame would compress trivially).
    _base_frame = np.zeros(FRAME_SHAPE, dtype=np.uint8)
    cv2.rectangle(_base_frame, (0, 0), (640, 200), (30, 80, 30), -1)
    cv2.putText(_base_frame, "BENCH", (30, 140), cv2.FONT_HERSHEY_SIMPLEX, 4, (0, 220, 0), 6)

    def producer_loop():
        interval  = 1.0 / PRODUCER_FPS
        stop_time = perf_counter() + DURATION_S
        frame_id  = 0

        while perf_counter() < stop_time and not error_event.is_set():
            t0 = perf_counter()

            slot = input_buf.acquire()
            if slot is None:
                stats["dropped_no_slot"] += 1
                print(f"[Producer] No free slot — frame {frame_id} dropped.")
                frame_id += 1
                elapsed = perf_counter() - t0
                sleep(max(0.0, interval - elapsed))
                continue

            # Stamp frame_id so consecutive frames differ and codec effort is realistic
            frame = _base_frame.copy()
            cv2.putText(frame, str(frame_id), (30, 300), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 4)
            input_buf.write(slot, frame)

            meta = AnnotationSlotMetadata(
                frame_id=frame_id,
                timestamp=time(),
                slot_index=slot,
                alert_msg="",
            )
            try:
                input_meta_q.put(meta, timeout=0.1)
                stats["produced"] += 1
            except QueueFullException:
                input_buf.release(slot)
                stats["dropped_queue_full"] += 1
                print(f"[Producer] Meta queue full — frame {frame_id} dropped.")

            frame_id += 1
            elapsed = perf_counter() - t0
            sleep(max(0.0, interval - elapsed))

        if TRIGGER_POISON_PILL:
            print(f"[Producer] Sending poison pill after {frame_id} frames.")
            input_meta_q.put(POISON_PILL)
        print(
            f"[Producer] Done.  produced={stats['produced']}  "
            f"dropped_no_slot={stats['dropped_no_slot']}  "
            f"dropped_queue_full={stats['dropped_queue_full']}"
        )

    def persistence_consumer():
        """Receives the video file path (or None) once VideoProducerProcess shuts down."""
        try:
            result = output_q.get(timeout=DURATION_S + 30)
            if result:
                print(f"[Persistence] Video saved: {result}")
            else:
                print("[Persistence] VideoProducerProcess reported a save failure (None received).")
        except Exception as e:
            print(f"[Persistence] Timed out or error waiting for video path: {e}")

    def error_trigger():
        sleep(DURATION_S)
        print("[ErrorTrigger] Setting error_event.")
        error_event.set()

    prod_thread = threading.Thread(target=producer_loop, daemon=True, name="BenchProducer")
    pers_thread = threading.Thread(target=persistence_consumer, daemon=True, name="BenchPersistence")

    print(f"[Main] Config: FPS={PRODUCER_FPS}  duration={DURATION_S}s  RTMP={'yes' if RTMP_URL else 'no'}")
    print("[Main] Starting VideoProducerProcess ...")
    process.start()
    sleep(0.5)  # let the process fully initialise before feeding it

    print("[Main] Starting persistence consumer ...")
    pers_thread.start()

    print("[Main] Starting producer ...")
    prod_thread.start()

    if TRIGGER_ERROR and not TRIGGER_POISON_PILL:
        threading.Thread(target=error_trigger, daemon=True).start()

    process.join()
    prod_thread.join(timeout=5.0)
    pers_thread.join(timeout=15.0)

    input_buf.unlink()

    total = stats["produced"]
    total_dropped = stats["dropped_no_slot"] + stats["dropped_queue_full"]
    print(
        f"\n[Main] === Summary ===\n"
        f"  Produced : {total}\n"
        f"  Dropped  : {total_dropped}  "
        f"(no_slot={stats['dropped_no_slot']}  queue_full={stats['dropped_queue_full']})\n"
        f"  Drop rate: {total_dropped / max(1, total + total_dropped) * 100:.1f}%\n"
        f"  Producer log : logs/video_out.log\n"
        f"  File sink    : logs/video_out_file.log   (h264_nvenc recording)\n"
        f"  Stream sink  : logs/video_out_stream.log (libx264 RTMP)\n"
    )
