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

from app.shared.processes.video_stream_manager import VideoStreamManager
from app.shared.processes.frame_buffer import FrameBuffer
from app.shared.processes.messages import AnnotationSlotMetadata
from app.shared.processes.constants import (
    FPS,
    PIPELINE_QUEUE_TIMEOUT,
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

    # ------- RTMP stream → media server (MediaMTX records on its side) --------
    media_server_url: str
    stream_manager_queue_max_size: PositiveInt = MAX_SIZE_VIDEO_STREAM
    stream_manager_ffmpeg_startup_timeout: PositiveFloat = VIDEO_OUT_STREAM_FFMPEG_STARTUP_TIMEOUT
    stream_manager_ffmpeg_shutdown_timeout: PositiveFloat = VIDEO_OUT_STREAM_FFMPEG_SHUTDOWN_TIMEOUT
    stream_manager_startup_timeout: PositiveFloat = VIDEO_OUT_STREAM_STARTUP_TIMEOUT
    stream_manager_shutdown_timeout: PositiveFloat = VIDEO_OUT_STREAM_SHUTDOWN_TIMEOUT


class VideoProducerProcess(mp.Process):
    """
    Terminal process in the pipeline's video branch.

    Reads AnnotationSlotMetadata from the input queue, copies each frame out of its
    SHM slot, releases the slot, and pushes the frame to VideoStreamManager which
    encodes and streams it to the media server via RTMP. The media server (MediaMTX)
    records the stream on its side; no local file is written here.

    VideoStreamManager owns a background thread and bounded queue so the slow
    FFmpeg encode/push never blocks the main loop. It reconnects automatically if
    the RTMP link drops.

    The sink is lazily started on the first frame, since FFmpeg needs the frame
    dimensions at launch.

    Termination:
    - Clean shutdown: POISON_PILL received on the input queue.
    - Error shutdown: error_event set by any process stops the loop immediately.
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: FrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: VideoProducerProcessConfig,
    ):
        super().__init__()

        self.input_meta_queue = input_meta_queue
        self.input_frame_buffer = input_frame_buffer
        self.error_event = error_event
        self.config = config

        self.work_finished = mp.Event()

        # Lazily started on the first frame (FFmpeg needs frame dims at launch).
        self.stream_manager: Optional[VideoStreamManager] = None

    def _start_sink(self, width: int, height: int):
        """Start the RTMP stream manager on the first frame, once dimensions are known."""
        self.stream_manager.set_frame_dims(width, height)
        if not self.stream_manager.start():
            logger.warning("VideoStreamManager failed to start; RTMP streaming disabled.")
            self.stream_manager = None

    def _shutdown(self):
        """Drain and finalize the RTMP stream."""
        if self.stream_manager is not None:
            self.stream_manager.stop()

    def run(self):

        logger.info("VideoProducerProcess started.")

        poison_pill_received = False

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

        sink_started = False

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

                # Lazy-start on the first frame (dims known only at runtime).
                if not sink_started:
                    h, w = frame.shape[:2]
                    self._start_sink(w, h)
                    sink_started = True

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

    from app.shared.processes.messages import AnnotationSlotMetadata
    from app.shared.processes.constants import POISON_PILL

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
    error_event  = mp.Event()
    input_meta_q = mp.Queue(maxsize=N_SLOTS)
    input_buf    = FrameBuffer(frame_shape=FRAME_SHAPE, n_slots=N_SLOTS)

    config = VideoProducerProcessConfig(
        fps=PRODUCER_FPS,
        media_server_url=RTMP_URL,
    )

    process = VideoProducerProcess(
        input_meta_queue=input_meta_q,
        input_frame_buffer=input_buf,
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

    print(f"[Main] Config: FPS={PRODUCER_FPS}  duration={DURATION_S}s  RTMP={'yes' if RTMP_URL else 'no'}")
    print("[Main] Starting VideoProducerProcess ...")
    process.start()
    sleep(0.5)  # let the process fully initialise before feeding it

    print("[Main] Starting producer ...")
    prod_thread.start()

    if TRIGGER_ERROR and not TRIGGER_POISON_PILL:
        threading.Thread(target=error_trigger, daemon=True).start()

    process.join()
    prod_thread.join(timeout=5.0)

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
        f"  Stream sink  : logs/video_out_stream.log (libx264 RTMP)\n"
    )
