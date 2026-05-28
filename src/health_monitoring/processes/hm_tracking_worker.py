import multiprocessing as mp
import multiprocessing.synchronize
import logging
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException
from time import time
from typing import Optional

from pydantic import BaseModel, NonNegativeInt, PositiveFloat

from src.health_monitoring.tracking.yolo_tracker import YOLOTracker
from src.health_monitoring.processes.messages import HMTrackingSlotMetadata
from src.shared.processes.messages import FrameSlotMetadata
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.constants import (
    PIPELINE_QUEUE_TIMEOUT,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
)


# ================================================================

logger = logging.getLogger("main.hm_tracking")

if not logger.handlers:
    _handler = logging.FileHandler('./logs/hm_tracking.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

# ================================================================


class HMTrackingWorkerConfig(BaseModel):
    """Configuration for HMTrackingWorker."""

    model_checkpoint: str
    track_kwargs: dict = {}     # YOLO inference params (conf, iou, tracker, imgsz, …)
    # Frames discarded between each tracked frame (0 = track every frame, 3 = 1 in 4).
    frame_skip: NonNegativeInt = 0
    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT


class HMTrackingWorker(mp.Process):
    """
    Tracking stage of the health monitoring pipeline.

    Reads a (H, W, 3) BGR frame at processing resolution from the input FrameBuffer,
    runs YOLO + BotSORT to produce active TrackState objects and a GMC homography H,
    writes the frame unchanged to the output FrameBuffer, and puts a
    HMTrackingSlotMetadata on the output queue carrying the slot index, tracks, and H.

    Termination:
    - Clean shutdown: POISON_PILL received on the input queue is propagated downstream.
    - Error shutdown: if error_event is set, the loop stops immediately.

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
        logger.info("HM tracking process started.")
        poison_pill_received = False

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

                if isinstance(meta, str) and meta == POISON_PILL:
                    logger.info("Found sentinel value on queue.")
                    poison_pill_received = True
                    break

                assert isinstance(meta, FrameSlotMetadata)

                # ---- frame skipping ----
                if _skip_countdown > 0:
                    _skip_countdown -= 1
                    self.input_frame_buffer.release(meta.slot_index)
                    logger.debug(f"Frame {meta.frame_id} skipped.")
                    continue
                _skip_countdown = self.config.frame_skip

                get_time = time() - iter_start

                # ---- zero-copy view of input slot ----
                predict_start = time()

                frame = self.input_frame_buffer.view(meta.slot_index)

                tracks, H = tracker.update(frame)

                predict_time = time() - predict_start

                logger.debug(f"Frame {meta.frame_id}: {len(tracks)} active tracks.")

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
                )
                try:
                    self.output_meta_queue.put(out_meta, timeout=self.config.queue_timeout)
                    logger.debug(f"Frame {meta.frame_id} → slot {out_slot}.")
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
                    f"GET: {get_time * 1000:.2f} ms, "
                    f"TRACK: {predict_time * 1000:.2f} ms, "
                    f"PROPAGATE: {(time() - append_start) * 1000:.2f} ms."
                )

            if not self.error_event.is_set():
                try:
                    logger.info("Attempting to put sentinel value on output queue ...")
                    self.output_meta_queue.put(POISON_PILL, timeout=self.config.poison_pill_timeout)
                    logger.info("Sentinel value passed to output queue.")
                except Exception as e:
                    logger.error(f"Error propagating Poison Pill: {e}")
                    self.error_event.set()
                    logger.warning(
                        "Error event set: force-stop application since downstream process "
                        "is unable to receive the poison pill."
                    )
            else:
                logger.info("Terminating and skipping Poison Pill sending. Error event is set.")

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in HM tracking process: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            self.input_frame_buffer.close()
            self.output_frame_buffer.close()

            logger.info(
                "HM tracking process terminated. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()
