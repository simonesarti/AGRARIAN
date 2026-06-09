import multiprocessing as mp
import multiprocessing.synchronize
import threading
import logging
from queue import Empty as QueueEmptyException, Queue, Full as QueueFullException
from time import time
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

    The main loop takes a zero-copy view of each SHM slot and hands (view, slot_index)
    to a background writer thread via an in-process queue — no numpy copy needed.
    The writer thread performs the slow I/O (cv2.VideoWriter.write + stream manager
    push) and releases the slot only after all reads are complete, following the same
    late-release zero-copy pattern used across the pipeline.

    Each frame is:
    - written to a local video file via cv2.VideoWriter
    - pushed to VideoStreamManager (background thread via FFmpeg) for RTMP streaming,
      if media_server_url is configured

    Both outputs are lazily initialised inside the writer thread on the first frame,
    since cv2.VideoWriter and FFmpeg both require frame dimensions at construction time.

    On clean shutdown (POISON_PILL received), the writer thread drains its queue before
    stopping so all buffered frames are encoded. The local video path is then passed to
    the downstream PersistenceProcess via output_queue for cloud upload.

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

        # Lazily initialised inside the writer thread on the first frame
        self.writer: Optional[cv2.VideoWriter] = None
        self.stream_manager: Optional[VideoStreamManager] = None
        self._video_filename: str = ""  # set at run() start from config

        # Background writer thread: receives (view, slot_index) tuples, performs
        # slow I/O, then releases the slot. Bounded by the SHM pool size so it
        # never accumulates more than MAX_SIZE_VIDEO_STREAM in-flight entries.
        self._writer_queue: Queue = Queue(maxsize=MAX_SIZE_VIDEO_STREAM)
        self._writer_thread: Optional[threading.Thread] = None
        self._writer_stop = threading.Event()

    def _init_writer(self, width: int, height: int):
        fourcc = cv2.VideoWriter_fourcc(*CODEC)
        self.writer = cv2.VideoWriter(
            filename=self._video_filename,
            fourcc=fourcc,
            fps=self.config.fps,
            frameSize=(width, height),
        )
        logger.info(f"VideoWriter initialised. File: '{self._video_filename}'. Frame size: {width}×{height}")

    def _writer_loop(self):
        """
        Background thread: dequeues (view, slot_index) pairs, writes to disk,
        pushes to stream manager, then releases the SHM slot.

        Exits only after draining the queue once _writer_stop is set, so all
        buffered frames are flushed before shutdown.
        """
        while True:
            try:
                item = self._writer_queue.get(timeout=0.01)
            except QueueEmptyException:
                if self._writer_stop.is_set():
                    break
                continue

            frame, put_time = item
            t_dequeue = time()
            h, w = frame.shape[:2]

            # Lazy-init on first frame (dimensions known only at runtime)
            if self.writer is None:
                self._init_writer(w, h)
            assert self.writer is not None
            if self.stream_manager is not None and self.stream_manager.stream_thread is None:
                self.stream_manager.set_frame_dims(w, h)
                if not self.stream_manager.start():
                    logger.warning("VideoStreamManager failed to start; RTMP streaming disabled.")
                    self.stream_manager = None

            self.writer.write(frame)
            t_write = time()

            if self.stream_manager:
                self.stream_manager.push_to_queue(frame)
            t_push = time()

            def ms(a, b): return f"{(b - a) * 1000:.1f}"
            logger.debug(
                f"[TIMING] "
                f"queue_lag={ms(put_time, t_dequeue)}ms | "
                f"write={ms(t_dequeue, t_write)}ms | "
                f"push={ms(t_write, t_push)}ms | "
                f"total={ms(put_time, t_push)}ms"
            )

    def _shutdown(self):
        """Drain writer thread, flush and close the video file, stop streaming, handoff to persistence."""

        # 1. Signal writer thread to drain remaining frames then stop; wait for it.
        self._writer_stop.set()
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=10.0)
            if self._writer_thread.is_alive():
                logger.warning("VideoWriterThread did not stop cleanly within timeout.")
                # Drain remaining items so the writer thread can exit cleanly.
                while not self._writer_queue.empty():
                    try:
                        self._writer_queue.get_nowait()
                    except QueueEmptyException:
                        break

        # 2. Finalize the local file immediately — before stopping the stream manager,
        #    so the moov atom is written even if the process is killed during stream cleanup.
        save_succeeded = False
        if self.writer:
            try:
                self.writer.release()
                logger.info(f"Recording saved at '{self._video_filename}'")
                save_succeeded = True
            except Exception as e:
                logger.error(f"Error saving recording: {e}")

        # 3. Stop the RTMP stream manager (can be slow; local file is already safe).
        if self.stream_manager:
            self.stream_manager.stop()

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
        self._writer_thread = None

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

        self._writer_stop.clear()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="VideoWriterThread",
            daemon=True,
        )
        self._writer_thread.start()

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

                # Copy frame out of SHM and release the slot immediately so the
                # upstream annotator can reuse it regardless of writer queue depth.
                frame = self.input_frame_buffer.read(meta.slot_index)
                self.input_frame_buffer.release(meta.slot_index)

                put_time = time()
                try:
                    self._writer_queue.put((frame, put_time), timeout=self.config.queue_timeout)
                    logger.debug(
                        f"[TIMING] frame={meta.frame_id} slot={meta.slot_index} | "
                        f"queue_put={(time() - put_time) * 1000:.1f}ms"
                    )
                except QueueFullException:
                    logger.warning(f"Writer queue full; frame {meta.frame_id} dropped for video output.")

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
    RTMP_URL = None  # e.g. "rtmp://localhost:1935/live/bench"

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
            except Full:
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
        f"  Per-frame timing: logs/video_out.log\n"
        f"  FFmpeg timing   : logs/video_out_stream.log\n"
    )
