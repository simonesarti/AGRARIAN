import multiprocessing as mp
import multiprocessing.synchronize
import logging
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException
from time import time, sleep

import cv2
import numpy as np
from pydantic import BaseModel, PositiveFloat

from src.danger_detection.utils import create_dangerous_intersections_masks
from src.danger_detection.output.frames import (
    draw_count,
    draw_dangerous_area,
    draw_detections,
    draw_safety_areas,
    get_danger_intersect_colored_frames,
)
from src.danger_detection.processes.messages import GeoSlotMetadata
from src.shared.processes.messages import AnnotationSlotMetadata
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.constants import (
    ANNOTATION_QUEUE_GET_TIMEOUT,
    ANNOTATION_QUEUE_PUT_TIMEOUT,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
    UPSAMPLING_MODE,
)


# ================================================================

logger = logging.getLogger("main.danger_annotation")

if not logger.handlers:  # Avoid duplicate handlers
    _handler = logging.FileHandler('./logs/danger_annotation.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

# ================================================================


class DangerAnnotationWorkerConfig(BaseModel):
    """Configuration for DangerAnnotationWorker."""

    queue_get_timeout: PositiveFloat = ANNOTATION_QUEUE_GET_TIMEOUT
    queue_put_timeout: PositiveFloat = ANNOTATION_QUEUE_PUT_TIMEOUT
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT


def annotate_frame(
        frame: np.ndarray,
        num_classes: int,
        classes_names: dict,
        classes: np.ndarray,
        boxes_centers: np.ndarray,
        boxes_corner1: np.ndarray,
        boxes_corner2: np.ndarray,
        safety_radius_pixels: int,
        danger_mask: np.ndarray,
        intersection_mask: np.ndarray,
        color_danger_frame: np.ndarray,
        color_intersect_frame: np.ndarray,
) -> np.ndarray:
    """Draw all danger and detection overlays onto the frame in-place and return it."""

    # draw safety circles around each detected animal
    if safety_radius_pixels > 0:
        draw_safety_areas(frame, boxes_centers, safety_radius_pixels)

    # Overlay dangerous areas (in red) and intersections (in yellow) on the annotated frame
    draw_dangerous_area(frame, danger_mask, intersection_mask, color_danger_frame, color_intersect_frame)

    # draw detection boxes
    draw_detections(frame, classes, boxes_corner1, boxes_corner2)

    # draw animal count
    draw_count(classes, num_classes, classes_names, frame)

    return frame


class DangerAnnotationWorker(mp.Process):
    """
    Combined danger analysis and frame annotation process in the danger detection pipeline.

    Reads a stacked (H, W, 8) slot from the upstream FrameBuffer:
        channels 0-2 : BGR frame (at processing resolution)
        channel  3   : roads_mask      (uint8, 0/1)
        channel  4   : vehicles_mask   (uint8, 0/1)
        channel  5   : nodata_dem_mask (uint8, 0/1)
        channel  6   : geofencing_mask (uint8, 0/1)
        channel  7   : slope_mask      (uint8, 0/1)

    Computes the danger mask and intersection mask by intersecting per-animal safety
    circles with the five danger layers. Annotates the frame in-place with safety
    circles, danger/intersection overlays, detection boxes, and animal count. Upscales
    the annotated frame to the original video resolution and writes it to the output
    FrameBuffer (sized (original_H, original_W, 3)).

    The output metadata carries the alert message string (empty if no danger), which the
    downstream alert writer uses to decide whether to dispatch a notification.

    Termination:
    - Clean shutdown: POISON_PILL received from the input metadata queue is
      propagated to the output metadata queue.
    - Error shutdown: if error_event is set by any process, the loop stops
      immediately without flushing.

    Frame drop policy: if no output buffer slot is free (consumer too slow) or
    the output metadata queue is full, the current frame is discarded.
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: FrameBuffer,
            output_meta_queue: mp.Queue,
            output_frame_buffer: FrameBuffer,  # must be sized (original_H, original_W, 3)
            error_event: multiprocessing.synchronize.Event,
            config: DangerAnnotationWorkerConfig,
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
        """
        Main loop of the process: processes frames combining danger detection and annotation.
        """
        logger.info("Danger annotation process started.")
        poison_pill_received = False

        # Lazily initialised on the first frame (need frame shape to allocate constant overlays).
        color_danger_frame = None    # (H, W, 3) red solid frame, precomputed once
        color_intersect_frame = None  # (H, W, 3) yellow solid frame, precomputed once

        try:

            while not self.error_event.is_set():

                iter_start = time()

                # ---- pull next frame metadata from the input queue ----
                try:
                    meta = self.input_meta_queue.get(timeout=self.config.queue_get_timeout)
                except QueueEmptyException:
                    logger.debug("Input queue timed out. Upstream producer may be stalled. Retrying ...")
                    continue

                # ---- poison pill: propagate downstream and exit ----
                if isinstance(meta, str) and meta == POISON_PILL:
                    logger.info("Found sentinel value on queue.")
                    poison_pill_received = True
                    break

                assert isinstance(meta, GeoSlotMetadata)

                get_start = time()

                # ---- read stacked (H, W, 8) slot and immediately release it ----
                # read() returns a copy, so we own this array and can modify it freely.
                stacked = self.input_frame_buffer.read(meta.slot_index)
                self.input_frame_buffer.release(meta.slot_index)

                # ---- split channels ----
                frame = stacked[:, :, :3]                  # BGR frame (processing resolution)
                roads_mask = stacked[:, :, 3]
                vehicles_mask = stacked[:, :, 4]
                nodata_dem_mask = stacked[:, :, 5]
                geofencing_mask = stacked[:, :, 6]
                slope_mask = stacked[:, :, 7]

                frame_height, frame_width = frame.shape[:2]

                # ---- lazy init of constant colour overlays (same size as processing frame) ----
                if color_danger_frame is None:
                    color_danger_frame, color_intersect_frame = get_danger_intersect_colored_frames(
                        shape=frame.shape
                    )
                    logger.info(f"Danger annotation process setup with frame size W×H = {frame_width}×{frame_height}")

                # ---- compute danger and intersection masks ----

                predict_start = time()

                # models outputs combining and processing
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

                danger_exists = len(danger_types) > 0
                alert_msg = " & ".join(danger_types) if danger_exists else ""

                # ---- annotate frame ----

                # Draws all overlays in-place on the processing-resolution frame.
                # Since stacked is our own copy from read(), this is safe.
                annotate_frame(
                    frame=frame,
                    num_classes=meta.num_classes,
                    classes_names=meta.classes_names,
                    classes=meta.classes,
                    boxes_centers=meta.boxes_centers,
                    boxes_corner1=meta.boxes_corner1,
                    boxes_corner2=meta.boxes_corner2,
                    safety_radius_pixels=meta.safety_radius_pixels,
                    danger_mask=danger_mask,
                    intersection_mask=intersection_mask,
                    color_danger_frame=color_danger_frame,
                    color_intersect_frame=color_intersect_frame,
                )

                # ---- upscale to original resolution ----

                # Resize to the original video resolution before writing to output buffer.
                # cv2.resize returns a new array, so the original stacked copy is unaffected.
                annotated_frame = cv2.resize(
                    src=frame,
                    dsize=meta.original_wh,     # (W, H) convention
                    interpolation=UPSAMPLING_MODE,
                )

                # ---- acquire an output slot and write the full-resolution annotated frame ----
                out_slot = self.output_frame_buffer.acquire()
                if out_slot is None:
                    logger.warning(
                        f"No free slot in output frame buffer. "
                        f"Frame {meta.frame_id} discarded. Consumer too slow?"
                    )
                    continue

                self.output_frame_buffer.write(out_slot, annotated_frame)

                # ---- build and enqueue output metadata ----
                out_meta = AnnotationSlotMetadata(
                    frame_id=meta.frame_id,
                    timestamp=meta.timestamp,
                    slot_index=out_slot,
                    alert_msg=alert_msg,
                )

                append_start = time()
                try:
                    self.output_meta_queue.put(out_meta, timeout=self.config.queue_put_timeout)
                    logger.debug(
                        f"Frame {meta.frame_id} → output slot {out_slot}. "
                        f"Danger: {alert_msg if danger_exists else 'none'}."
                    )
                except QueueFullException:
                    # Return the output slot so it is not leaked
                    self.output_frame_buffer.release(out_slot)
                    logger.error(
                        f"Output metadata queue full. Frame {meta.frame_id} discarded. "
                        "Consumer too slow or stopped?"
                    )

                iter_end = time()

                logger.debug(
                    f"frame {meta.frame_id} processed in {(iter_end - iter_start) * 1000:.2f} ms, "
                    f"of which --> "
                    f"GET: {(predict_start - get_start) * 1000:.2f} ms, "
                    f"PROCESS: {(append_start - predict_start) * 1000:.2f} ms, "
                    f"PROPAGATE: {(iter_end - append_start) * 1000:.2f} ms."
                )
                # iteration completed correctly, move on to process next frame

            # Propagate termination signal via poison pill on clean shutdown.
            if not self.error_event.is_set():
                try:
                    logger.info("Attempting to put sentinel value on output queue ...")
                    self.output_meta_queue.put(POISON_PILL, timeout=self.config.poison_pill_timeout)
                    logger.info("Sentinel value has been passed on to the next process.")
                except Exception as e:
                    logger.error(f"Error propagating Poison Pill: {e}")
                    self.error_event.set()
                    logger.warning(
                        "Error event set: force-stop application since downstream processes "
                        "are unable to receive the poison pill."
                    )
            else:
                logger.info("Terminating and skipping Poison Pill sending. Error event is set.")

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in danger annotation process: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:

            self.input_frame_buffer.close()
            self.output_frame_buffer.close()

            logger.info(
                "Danger annotation process terminated successfully. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()


if __name__ == "__main__":

    import threading

    STACKED_GEO_SHAPE = (720, 1280, 8)     # (H, W, frame+all_masks) — input slot
    ORIGINAL_WH = (1920, 1080)              # original video resolution (W, H)
    ANNOTATED_SHAPE = (ORIGINAL_WH[1], ORIGINAL_WH[0], 3)  # (H, W, C) for output slot

    N_SLOTS = 3
    N_FRAMES = 10

    error_event = mp.Event()

    input_meta_queue = mp.Queue(maxsize=N_SLOTS)
    input_frame_buffer = FrameBuffer(frame_shape=STACKED_GEO_SHAPE, n_slots=N_SLOTS)

    output_meta_queue = mp.Queue(maxsize=N_SLOTS)
    output_frame_buffer = FrameBuffer(frame_shape=ANNOTATED_SHAPE, n_slots=N_SLOTS)

    config = DangerAnnotationWorkerConfig()

    worker = DangerAnnotationWorker(
        input_meta_queue=input_meta_queue,
        input_frame_buffer=input_frame_buffer,
        output_meta_queue=output_meta_queue,
        output_frame_buffer=output_frame_buffer,
        error_event=error_event,
        config=config,
    )

    def producer_loop():
        """Push fake GeoSlotMetadata into the input shared memory buffer."""
        for i in range(N_FRAMES):
            slot = input_frame_buffer.acquire()
            if slot is not None:
                # channels 0-2: random BGR frame; channels 3-7: random binary masks
                stacked = np.zeros(STACKED_GEO_SHAPE, dtype=np.uint8)
                stacked[:, :, :3] = np.random.randint(0, 256, (STACKED_GEO_SHAPE[0], STACKED_GEO_SHAPE[1], 3), dtype=np.uint8)
                for ch in range(3, 8):
                    stacked[:, :, ch] = np.random.randint(0, 2, (STACKED_GEO_SHAPE[0], STACKED_GEO_SHAPE[1]), dtype=np.uint8)
                input_frame_buffer.write(slot, stacked)

                n_animals = 5
                centers = np.array([[200 + j * 100, 300] for j in range(n_animals)], dtype=np.int32)
                meta = GeoSlotMetadata(
                    frame_id=i,
                    timestamp=time(),
                    original_wh=ORIGINAL_WH,
                    slot_index=slot,
                    telemetry=None,
                    classes_names={0: "sheep", 1: "goat"},
                    num_classes=2,
                    classes=np.zeros(n_animals, dtype=np.int32),
                    boxes_centers=centers,
                    boxes_corner1=centers - 20,
                    boxes_corner2=centers + 20,
                    safety_radius_pixels=80,
                )
                try:
                    input_meta_queue.put(meta, timeout=1.0)
                except Exception:
                    input_frame_buffer.release(slot)
            sleep(1 / 10)
        input_meta_queue.put(POISON_PILL)

    def consumer_loop():
        """Drain the output queue and release output slots."""
        frames_received = 0
        while True:
            try:
                msg = output_meta_queue.get(timeout=10.0)
            except Exception:
                print("[Consumer] Timed out. Stopping.")
                break
            if isinstance(msg, str) and msg == POISON_PILL:
                output_meta_queue.put(POISON_PILL)
                print(f"[Consumer] Poison pill received. {frames_received} frames processed.")
                break
            if error_event.is_set():
                break
            assert isinstance(msg, AnnotationSlotMetadata)
            annotated = output_frame_buffer.read(msg.slot_index)
            output_frame_buffer.release(msg.slot_index)
            frames_received += 1
            print(
                f"[Consumer] frame_id={msg.frame_id} "
                f"annotated_shape={annotated.shape} "
                f"alert='{msg.alert_msg}' "
                f"slot={msg.slot_index}"
            )

    prod_thread = threading.Thread(target=producer_loop, daemon=True)
    cons_thread = threading.Thread(target=consumer_loop, daemon=True)

    print("[Main] Starting worker ...")
    worker.start()
    sleep(0.5)

    print("[Main] Starting consumer ...")
    cons_thread.start()

    print("[Main] Starting producer ...")
    prod_thread.start()

    worker.join()
    prod_thread.join(timeout=5.0)
    cons_thread.join(timeout=5.0)

    input_frame_buffer.unlink()
    output_frame_buffer.unlink()
    print("[Main] Done.")
