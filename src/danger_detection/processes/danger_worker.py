import multiprocessing as mp
import multiprocessing.synchronize
import logging
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException
from time import time

import numpy as np
from pydantic import BaseModel, PositiveFloat

from src.danger_detection.utils import create_dangerous_intersections_masks
from src.danger_detection.processes.messages import GeoSlotMetadata, DangerSlotMetadata
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.constants import (
    PIPELINE_QUEUE_TIMEOUT,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
)


# ================================================================

logger = logging.getLogger("main.danger_detection")

if not logger.handlers:
    _handler = logging.FileHandler('./logs/danger_detection.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.WARNING)

# ================================================================


class DangerWorkerConfig(BaseModel):
    """Configuration for DangerWorker."""

    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT
    cpu_affinity: int | None = None


class DangerWorker(mp.Process):
    """
    Danger detection stage of the danger detection pipeline.

    Reads a (H, W, 8) stacked slot from GeoWorker:
        channels 0-2 : BGR frame (processing resolution)
        channel  3   : roads_mask
        channel  4   : vehicles_mask
        channel  5   : nodata_dem_mask
        channel  6   : geofencing_mask
        channel  7   : slope_mask

    Intersects per-animal safety circles with each danger layer to compute the
    danger_mask, intersection_mask, and the list of active danger types.

    Writes a (H, W, 5) stacked slot to AnnotationWorker:
        channels 0-2 : BGR frame (unchanged)
        channel  3   : danger_mask
        channel  4   : intersection_mask

    The alert_msg string (e.g. "Roads & Vehicles") travels in DangerSlotMetadata.
    
    Termination:
    - Clean shutdown: POISON_PILL received from the input metadata queue is
      propagated to both output metadata queues.
    - Error shutdown: if error_event is set by any process, the loop stops
      immediately without flushing.

    Frame drop policy: if no output buffer slot is free (consumer too slow) or
    the output metadata queue is full, the current frame is discarded for that
    consumer only.
    
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: FrameBuffer,
            output_meta_queue: mp.Queue,
            output_frame_buffer: FrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: DangerWorkerConfig,
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
        from src.shared.processes.cpu_affinity import pin_to_core
        pin_to_core(self.config.cpu_affinity)
        logger.info("Danger detection process started.")
        poison_pill_received = False

        try:

            while not self.error_event.is_set():

                iter_start = time()

                # ---- pull next frame metadata from the input queue ----
                try:
                    meta = self.input_meta_queue.get(timeout=self.config.queue_timeout)
                except QueueEmptyException:
                    logger.debug("Input queue timed out. Upstream producer may be stalled. Retrying ...")
                    continue

                # ---- poison pill: propagate downstream and exit ----
                if isinstance(meta, str) and meta == POISON_PILL:
                    logger.info("Found sentinel value on queue.")
                    poison_pill_received = True
                    break

                assert isinstance(meta, GeoSlotMetadata)

                get_time = time() - iter_start

                # ---- zero-copy view of input slot ----
                
                collect_start = time()

                stacked = self.input_frame_buffer.view(meta.slot_index)

                frame = stacked[:, :, :3]
                # Gather all 5 mask channels in a single contiguous copy to avoid
                # repeated scatter-gather over strided SHM pages in create_dangerous_intersections_masks.
                masks = np.ascontiguousarray(stacked[:, :, 3:])
                roads_mask       = masks[:, :, 0]
                vehicles_mask    = masks[:, :, 1]
                nodata_dem_mask  = masks[:, :, 2]
                geofencing_mask  = masks[:, :, 3]
                slope_mask       = masks[:, :, 4]

                frame_height, frame_width = frame.shape[:2]

                detect_start = time()

                danger_mask, intersection_mask, danger_types = create_dangerous_intersections_masks(
                    frame_height=frame_height,
                    frame_width=frame_width,
                    boxes_centers=meta.boxes_centers,
                    safety_radius_pixels=meta.safety_radius_pixels,
                    segment_roads_danger_mask=roads_mask,
                    segment_vehicles_danger_mask=vehicles_mask,
                    dem_nodata_danger_mask=nodata_dem_mask,
                    geofencing_danger_mask=geofencing_mask,
                    slope_danger_mask=slope_mask,
                )

                alert_msg = " & ".join(danger_types) if danger_types else ""

                collect_time = detect_start - collect_start
                detect_time = time() - detect_start

                # ---- write (H, W, 5) output slot ----
                append_start = time()

                out_slot = self.output_frame_buffer.acquire()
                if out_slot is None:
                    self.input_frame_buffer.release(meta.slot_index)
                    logger.warning(
                        f"No free slot in output frame buffer. "
                        f"Frame {meta.frame_id} dropped. Consumer too slow?"
                    )
                    logger.debug(
                        f"frame {meta.frame_id} processed in {(time() - iter_start) * 1000:.2f} ms, "
                        f"of which --> GET: {get_time * 1000:.2f} ms, "
                        f"COLLECT: {collect_time * 1000:.2f} ms, "
                        f"DETECT: {detect_time * 1000:.2f} ms, "
                        f"PROPAGATE: skipped (no slot)."
                    )
                    continue

                # Write directly into the output slot — eliminates the 4.61 MB
                # intermediate array that np.concatenate would allocate.
                # Input slot must be held until frame data is copied into output slot.
                dst = self.output_frame_buffer.view(out_slot)
                np.copyto(dst[:, :, :3], frame)
                dst[:, :, 3] = danger_mask
                dst[:, :, 4] = intersection_mask
                self.input_frame_buffer.release(meta.slot_index)

                out_meta = DangerSlotMetadata(
                    frame_id=meta.frame_id,
                    timestamp=meta.timestamp,
                    original_wh=meta.original_wh,
                    slot_index=out_slot,
                    classes_names=meta.classes_names,
                    num_classes=meta.num_classes,
                    classes=meta.classes,
                    boxes_centers=meta.boxes_centers,
                    boxes_corner1=meta.boxes_corner1,
                    boxes_corner2=meta.boxes_corner2,
                    safety_radius_pixels=meta.safety_radius_pixels,
                    alert_msg=alert_msg,
                )

                try:
                    self.output_meta_queue.put(out_meta, timeout=self.config.queue_timeout)
                    logger.debug(
                        f"Frame {meta.frame_id} → output slot {out_slot}. "
                        f"Danger: '{alert_msg if alert_msg else 'none'}'."
                    )
                except QueueFullException:
                    self.output_frame_buffer.release(out_slot)
                    logger.warning(
                        f"Output metadata queue full. Frame {meta.frame_id} dropped. Consumer too slow or stopped?"
                    )

                iter_end = time()
                logger.debug(
                    f"frame {meta.frame_id} processed in {(iter_end - iter_start) * 1000:.2f} ms, "
                    f"of which --> "
                    f"GET: {get_time * 1000:.2f} ms, "
                    f"COLLECT: {collect_time * 1000:.2f} ms, "
                    f"DETECT: {detect_time * 1000:.2f} ms, "
                    f"PROPAGATE: {(iter_end - append_start) * 1000:.2f} ms."
                )

            if not self.error_event.is_set():
                try:
                    logger.info("Attempting to put sentinel value on output queue ...")
                    self.output_meta_queue.put(POISON_PILL, timeout=self.config.poison_pill_timeout)
                    logger.info("Sentinel value passed to output queue.")
                except Exception as e:
                    logger.error(f"Error propagating Poison Pill: {e}")
                    self.error_event.set()
                    logger.warning("Error event set: force-stop application.")
            else:
                logger.info("Terminating and skipping Poison Pill sending. Error event is set.")

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in danger detection process: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            self.input_frame_buffer.close()
            self.output_frame_buffer.close()

            logger.info(
                "Danger detection process terminated. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()
