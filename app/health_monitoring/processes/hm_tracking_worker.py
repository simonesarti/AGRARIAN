import multiprocessing as mp
import multiprocessing.synchronize
import logging
from queue import Empty as QueueEmptyException, Full as QueueFullException
from time import time
from typing import Optional

from pydantic import BaseModel, NonNegativeInt, PositiveFloat

from app.health_monitoring.tracking.yolo_tracker import YOLOTracker
from app.health_monitoring.processes.messages import HMTrackingSlotMetadata
from app.shared.processes.messages import FrameSlotMetadata
from app.shared.processes.frame_buffer import FrameBuffer
from app.shared.processes.signals import reset_child_signal_handlers
from app.shared.processes.constants import PIPELINE_QUEUE_TIMEOUT
from app.shared.processes.logging_config import worker_log_level


# ================================================================

logger = logging.getLogger("main.hm_tracking")

if not logger.handlers:
    _handler = logging.FileHandler('./logs/hm_tracking.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(worker_log_level())

# ================================================================


class HMTrackingWorkerConfig(BaseModel):
    """Configuration for HMTrackingWorker."""

    model_checkpoint: str
    track_kwargs: dict = {}     # YOLO inference params (conf, iou, tracker, imgsz, …)
    # Frames discarded between each tracked frame (0 = track every frame, 3 = 1 in 4).
    frame_skip: NonNegativeInt = 0
    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT


class HMTrackingWorker(mp.Process):
    """
    Tracking stage of the health monitoring pipeline.

    Reads a (H, W, 3) BGR frame at processing resolution from the input FrameBuffer,
    runs YOLO + BotSORT to produce active TrackState objects and a GMC homography H,
    writes the frame unchanged to the output FrameBuffer, and puts a
    HMTrackingSlotMetadata on the output queue carrying the slot index, tracks, and H.

    Termination:
    - error_event is the only stop signal: the loop exits immediately when it is set.
      An idle input queue means the video source is temporarily unavailable, so the
      worker keeps waiting rather than shutting down.

    Frame drop policy: if no output buffer slot is free or the output queue is full,
    the current frame is discarded.
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: FrameBuffer,
            output_meta_queue: mp.Queue,
            output_frame_buffer: FrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: HMTrackingWorkerConfig,
    ):
        super().__init__()

        self.input_meta_queue = input_meta_queue
        self.input_frame_buffer = input_frame_buffer
        self.output_meta_queue = output_meta_queue
        self.output_frame_buffer = output_frame_buffer
        self.error_event = error_event
        self.config = config

        self.work_finished = mp.Event()

    def run(self):

        # Drop the SIGTERM/SIGINT handlers inherited from the orchestrator at fork.
        reset_child_signal_handlers()

        logger.info("HM tracking process started.")

        try:

            tracker = YOLOTracker(
                model_checkpoint=self.config.model_checkpoint,
                track_kwargs=self.config.track_kwargs,
            )
            tracker.load()
            logger.info("YOLO tracker loaded.")

            _skip_countdown = 0

            while not self.error_event.is_set():

                iter_start = time()

                try:
                    meta = self.input_meta_queue.get(timeout=self.config.queue_timeout)
                except QueueEmptyException:
                    logger.debug("Input queue timed out. Upstream producer may be stalled. Retrying ...")
                    continue

                assert isinstance(meta, FrameSlotMetadata)

                # ---- zero-copy view of input slot ----
                frame = self.input_frame_buffer.view(meta.slot_index)

                # ---- run tracker on keyframes; assign empty outputs for passthrough ----
                predict_start = time()
                if _skip_countdown == 0:
                    _skip_countdown = self.config.frame_skip
                    tracks, H = tracker.update(frame)
                    is_keyframe = True
                    logger.debug(f"Frame {meta.frame_id}: {len(tracks)} active tracks.")
                else:
                    _skip_countdown -= 1
                    tracks, H = [], None
                    is_keyframe = False
                predict_time = time() - predict_start

                # ---- write frame to output buffer ----
                append_start = time()

                out_slot = self.output_frame_buffer.acquire()
                if out_slot is None:
                    self.input_frame_buffer.release(meta.slot_index)
                    logger.warning(
                        f"No free slot in output frame buffer. "
                        f"Frame {meta.frame_id} dropped. Consumer too slow?"
                    )
                    continue

                self.output_frame_buffer.write(out_slot, frame)
                self.input_frame_buffer.release(meta.slot_index)
                out_meta = HMTrackingSlotMetadata(
                    frame_id=meta.frame_id,
                    timestamp=meta.timestamp,
                    original_wh=meta.original_wh,
                    slot_index=out_slot,
                    tracks=tracks,
                    H=H,
                    is_keyframe=is_keyframe,
                )
                try:
                    self.output_meta_queue.put(out_meta, timeout=self.config.queue_timeout)
                    logger.debug(f"Frame {meta.frame_id} → slot {out_slot} (keyframe={is_keyframe}).")
                except QueueFullException:
                    self.output_frame_buffer.release(out_slot)
                    logger.warning(
                        f"Output metadata queue full. Frame {meta.frame_id} dropped. "
                        "Consumer too slow or stopped?"
                    )

                iter_time = time() - iter_start
                logger.debug(
                    f"frame {meta.frame_id} processed in {iter_time * 1000:.2f} ms, "
                    f"of which --> "
                    f"TRACK: {predict_time * 1000:.2f} ms, "
                    f"PROPAGATE: {(time() - append_start) * 1000:.2f} ms."
                )

            logger.info("Error event observed. Stopping the HM tracking worker.")

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in HM tracking process: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            self.input_frame_buffer.close()
            self.output_frame_buffer.close()

            logger.info(
                "HM tracking process terminated. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()
