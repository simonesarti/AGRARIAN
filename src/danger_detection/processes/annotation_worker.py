import multiprocessing as mp
import multiprocessing.synchronize
import logging
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException
from time import time

import cv2
import numpy as np
from pydantic import BaseModel, PositiveFloat

from src.danger_detection.output.frames import (
    draw_count,
    draw_dangerous_area,
    draw_detections,
    draw_safety_areas,
    get_danger_intersect_colored_frames,
)
from src.danger_detection.processes.messages import DangerSlotMetadata
from src.shared.processes.messages import AnnotationSlotMetadata
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.constants import (
    PIPELINE_QUEUE_TIMEOUT,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
)


# ================================================================

logger = logging.getLogger("main.danger_annotation")

if not logger.handlers:
    _handler = logging.FileHandler('./logs/danger_annotation.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

# ================================================================


class AnnotationWorkerConfig(BaseModel):
    """Configuration for AnnotationWorker."""

    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT


class AnnotationWorker(mp.Process):
    """
    Annotation stage of the danger detection pipeline.

    Reads a (H, W, 5) stacked slot from DangerWorker:
        channels 0-2 : BGR frame (processing resolution)
        channel  3   : danger_mask
        channel  4   : intersection_mask

    Draws all overlays (safety circles, danger/intersection areas, detection boxes,
    animal count) on the processing-resolution frame, then upscales to original
    video resolution.

    Fan-out: the annotated frame is written independently to two output FrameBuffers —
    one for the alert writer and one for the video writer.
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: FrameBuffer,
            alert_output_meta_queue: mp.Queue,
            alert_output_frame_buffer: FrameBuffer,
            video_output_meta_queue: mp.Queue,
            video_output_frame_buffer: FrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: AnnotationWorkerConfig,
    ):
        super().__init__()

        self.input_meta_queue = input_meta_queue
        self.input_frame_buffer = input_frame_buffer
        self.alert_output_meta_queue = alert_output_meta_queue
        self.alert_output_frame_buffer = alert_output_frame_buffer
        self.video_output_meta_queue = video_output_meta_queue
        self.video_output_frame_buffer = video_output_frame_buffer
        self.error_event = error_event
        self.config = config

        self.work_finished = mp.Event()

    def run(self):
        logger.info("Danger annotation process started.")
        poison_pill_received = False

        color_danger_frame = None
        color_intersect_frame = None
        danger_buf = None
        overlay_buf = None

        try:

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

                assert isinstance(meta, DangerSlotMetadata)

                get_start = time()

                # ---- zero-copy view of input slot ----
                stacked = self.input_frame_buffer.view(meta.slot_index)

                frame            = np.ascontiguousarray(stacked[:, :, :3])
                danger_mask      = stacked[:, :, 3]
                intersection_mask = stacked[:, :, 4]

                frame_height, frame_width = frame.shape[:2]

                if color_danger_frame is None:
                    color_danger_frame, color_intersect_frame, danger_buf, overlay_buf = get_danger_intersect_colored_frames(shape=frame.shape)
                    logger.info(f"Annotation process setup with frame size W×H = {frame_width}×{frame_height}")

                # ---- annotate ----
                annotate_start = time()

                if meta.safety_radius_pixels > 0:
                    draw_safety_areas(frame, meta.boxes_centers, meta.safety_radius_pixels)

                draw_dangerous_area(frame, danger_mask, intersection_mask, color_danger_frame, color_intersect_frame, danger_buf, overlay_buf)
                self.input_frame_buffer.release(meta.slot_index)
                draw_detections(frame, meta.classes, meta.boxes_corner1, meta.boxes_corner2)
                draw_count(meta.classes, meta.num_classes, meta.classes_names, frame)
                
                # ---- upscale to original resolution ----
                annotated_frame = cv2.resize(
                    src=frame,
                    dsize=meta.original_wh,
                    interpolation=cv2.INTER_LINEAR,
                )

                # ---- fan-out: write annotated frame to both consumers independently----
                append_start = time()

                danger_exists = bool(meta.alert_msg)

                # -- Alert output --
                alert_slot = self.alert_output_frame_buffer.acquire()
                if alert_slot is None:
                    logger.warning(
                        f"No free slot in alert output frame buffer. "
                        f"Frame {meta.frame_id} dropped for alert writer. Consumer too slow?"
                    )
                else:
                    self.alert_output_frame_buffer.write(alert_slot, annotated_frame)
                    alert_meta = AnnotationSlotMetadata(
                        frame_id=meta.frame_id,
                        timestamp=meta.timestamp,
                        slot_index=alert_slot,
                        alert_msg=meta.alert_msg,
                    )
                    try:
                        self.alert_output_meta_queue.put(alert_meta, timeout=self.config.queue_timeout)
                        logger.debug(
                            f"Frame {meta.frame_id} → alert slot {alert_slot}. "
                            f"Danger: {meta.alert_msg if danger_exists else 'none'}."
                        )
                    except QueueFullException:
                        self.alert_output_frame_buffer.release(alert_slot)
                        logger.error(
                            f"Alert output metadata queue full. Frame {meta.frame_id} dropped for alert writer. Consumer too slow or stopped?"
                        )

                # -- Video output --
                video_slot = self.video_output_frame_buffer.acquire()
                if video_slot is None:
                    logger.warning(
                        f"No free slot in video output frame buffer. "
                        f"Frame {meta.frame_id} dropped for video writer. Consumer too slow?"
                    )
                else:
                    self.video_output_frame_buffer.write(video_slot, annotated_frame)
                    video_meta = AnnotationSlotMetadata(
                        frame_id=meta.frame_id,
                        timestamp=meta.timestamp,
                        slot_index=video_slot,
                        alert_msg=meta.alert_msg,
                    )
                    try:
                        self.video_output_meta_queue.put(video_meta, timeout=self.config.queue_timeout)
                        logger.debug(
                            f"Frame {meta.frame_id} → video slot {video_slot}. "
                            f"Danger: {meta.alert_msg if danger_exists else 'none'}."
                        )
                    except QueueFullException:
                        self.video_output_frame_buffer.release(video_slot)
                        logger.error(
                            f"Video output metadata queue full. Frame {meta.frame_id} dropped for video writer. Consumer too slow or stopped?"
                        )

                iter_end = time()
                logger.debug(
                    f"frame {meta.frame_id} processed in {(iter_end - iter_start) * 1000:.2f} ms, "
                    f"of which --> "
                    f"GET: {(annotate_start - get_start) * 1000:.2f} ms, "
                    f"ANNOTATE: {(append_start - annotate_start) * 1000:.2f} ms, "
                    f"PROPAGATE: {(iter_end - append_start) * 1000:.2f} ms."
                )

            if not self.error_event.is_set():
                for name, q in [
                    ("alert", self.alert_output_meta_queue),
                    ("video", self.video_output_meta_queue),
                ]:
                    try:
                        logger.info(f"Attempting to put sentinel value on {name} output queue ...")
                        q.put(POISON_PILL, timeout=self.config.poison_pill_timeout)
                        logger.info(f"Sentinel value passed to {name} output queue.")
                    except Exception as e:
                        logger.error(f"Error propagating Poison Pill to {name} output queue: {e}")
                        self.error_event.set()
                        logger.warning(
                            "Error event set: force-stop application since downstream process "
                            "is unable to receive the poison pill."
                        )
            else:
                logger.info("Terminating and skipping Poison Pill sending. Error event is set.")

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in danger annotation process: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            self.input_frame_buffer.close()
            self.alert_output_frame_buffer.close()
            self.video_output_frame_buffer.close()

            logger.info(
                "Danger annotation process terminated. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()
