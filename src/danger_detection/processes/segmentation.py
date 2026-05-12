import multiprocessing as mp
import multiprocessing.synchronize
import logging
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException
from time import time, sleep

import numpy as np
from pydantic import BaseModel, Field, PositiveFloat, field_validator

from src.danger_detection.segmentation.segmentation import create_onnx_segmentation_session, perform_segmentation
from src.danger_detection.processes.messages import DetectionSlotMetadata, SegmentationSlotMetadata
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.constants import (
    MODELS_QUEUE_GET_TIMEOUT,
    MODELS_QUEUE_PUT_TIMEOUT,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
)


# ================================================================

logger = logging.getLogger("main.danger_segmentation")

if not logger.handlers:  # Avoid duplicate handlers
    _handler = logging.FileHandler('./logs/danger_segmentation.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

# ================================================================


class SegmentationWorkerConfig(BaseModel):
    """Configuration for SegmentationWorker."""

    model_checkpoint: str
    predict_args: dict = Field(default_factory=dict)

    queue_get_timeout: PositiveFloat = MODELS_QUEUE_GET_TIMEOUT
    queue_put_timeout: PositiveFloat = MODELS_QUEUE_PUT_TIMEOUT
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT

    @field_validator('model_checkpoint')
    @classmethod
    def must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("model_checkpoint must not be empty")
        return v


class SegmenterWrapper:

    def __init__(self, onnx_session, onnx_input_name, onnx_input_shape, predict_args):
        self.onnx_session = onnx_session
        self.onnx_input_name = onnx_input_name
        self.onnx_input_shape = onnx_input_shape
        self.predict_args = predict_args

    def predict(self, frame):
        return perform_segmentation(
            session=self.onnx_session,
            input_name=self.onnx_input_name,
            frame=frame,
            segmentation_args=self.predict_args,
        )


class SegmentationWorker(mp.Process):
    """
    Segmentation process in the danger detection pipeline.

    Reads frames from the upstream FrameBuffer (H, W, 3), runs ONNX road/vehicle
    segmentation, and writes a stacked (H, W, 5) array to the downstream FrameBuffer:
        channels 0-2 : BGR frame (forwarded unchanged)
        channel  3   : roads_mask  (uint8, values 0/1)
        channel  4   : vehicles_mask (uint8, values 0/1)

    Detection metadata (bounding boxes, class IDs) received from DetectionSlotMetadata
    is forwarded unchanged in SegmentationSlotMetadata alongside the new slot reference.

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
            output_frame_buffer: FrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: SegmentationWorkerConfig,
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
        Main loop of the process: instantiates the segmenter once, then processes frames.
        """
        logger.info("Roads & vehicles segmentation process started.")
        poison_pill_received = False

        try:

            # Instantiate the ONNX session inside run() so the session is created in the
            # child process and not inherited/pickled from the parent.
            segmenter_session, segmenter_input_name, segmenter_input_shape = (
                create_onnx_segmentation_session(self.config.model_checkpoint)
            )
            segmenter = SegmenterWrapper(
                onnx_session=segmenter_session,
                onnx_input_name=segmenter_input_name,
                onnx_input_shape=segmenter_input_shape,
                predict_args=self.config.predict_args,
            )
            logger.info("Roads & vehicles segmentation model loaded.")

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

                assert isinstance(meta, DetectionSlotMetadata)

                get_start = time()

                # ---- read frame from input shared memory and immediately release the slot ----
                frame = self.input_frame_buffer.read(meta.slot_index)
                self.input_frame_buffer.release(meta.slot_index)

                # ---- run ONNX segmentation ----
                predict_start = time()
                roads_mask, vehicles_mask = segmenter.predict(frame)

                # ---- stack frame and masks into a single (H, W, 5) array ----
                # Roads and vehicles masks come out as (H, W); add channel dim before stacking.
                stacked = np.concatenate(
                    [
                        frame,
                        roads_mask[:, :, np.newaxis],
                        vehicles_mask[:, :, np.newaxis],
                    ],
                    axis=2,
                )

                # ---- acquire an output slot and write the stacked array ----
                out_slot = self.output_frame_buffer.acquire()
                if out_slot is None:
                    logger.warning(
                        f"No free slot in output frame buffer. "
                        f"Frame {meta.frame_id} discarded. Consumer too slow?"
                    )
                    continue

                self.output_frame_buffer.write(out_slot, stacked)

                # ---- build and enqueue output metadata ----
                out_meta = SegmentationSlotMetadata(
                    frame_id=meta.frame_id,
                    timestamp=meta.timestamp,
                    original_wh=meta.original_wh,
                    slot_index=out_slot,
                    telemetry=meta.telemetry,
                    classes_names=meta.classes_names,
                    num_classes=meta.num_classes,
                    classes=meta.classes,
                    boxes_centers=meta.boxes_centers,
                    boxes_corner1=meta.boxes_corner1,
                    boxes_corner2=meta.boxes_corner2,
                )

                append_start = time()
                try:
                    self.output_meta_queue.put(out_meta, timeout=self.config.queue_put_timeout)
                    logger.debug(
                        f"Frame {meta.frame_id} → output slot {out_slot}, "
                        f"segmentation results queued."
                    )
                except QueueFullException:
                    # Return the output slot so the consumer does not leak it
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
                    f"PREDICT: {(append_start - predict_start) * 1000:.2f} ms, "
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
            logger.critical(f"An unexpected critical error happened in the segmentation process: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:

            self.input_frame_buffer.close()
            self.output_frame_buffer.close()

            logger.info(
                "Roads & vehicles segmentation process terminated successfully. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()


if __name__ == "__main__":

    import threading
    from src.shared.processes.messages import CombinedSlotMetadata

    FRAME_SHAPE = (720, 1280, 3)   # (H, W, C) — input frame
    STACKED_SHAPE = (720, 1280, 5)  # (H, W, C+masks) — output slot
    N_SLOTS = 3
    N_FRAMES = 10

    error_event = mp.Event()

    input_meta_queue = mp.Queue(maxsize=N_SLOTS)
    input_frame_buffer = FrameBuffer(frame_shape=FRAME_SHAPE, n_slots=N_SLOTS)

    output_meta_queue = mp.Queue(maxsize=N_SLOTS)
    output_frame_buffer = FrameBuffer(frame_shape=STACKED_SHAPE, n_slots=N_SLOTS)

    config = SegmentationWorkerConfig(
        model_checkpoint="models/segmenter.onnx",
    )

    worker = SegmentationWorker(
        input_meta_queue=input_meta_queue,
        input_frame_buffer=input_frame_buffer,
        output_meta_queue=output_meta_queue,
        output_frame_buffer=output_frame_buffer,
        error_event=error_event,
        config=config,
    )

    def producer_loop():
        """Push fake DetectionSlotMetadata frames into the input shared memory buffer."""
        for i in range(N_FRAMES):
            slot = input_frame_buffer.acquire()
            if slot is not None:
                frame = np.random.randint(0, 256, FRAME_SHAPE, dtype=np.uint8)
                input_frame_buffer.write(slot, frame)
                meta = DetectionSlotMetadata(
                    frame_id=i,
                    timestamp=time(),
                    original_wh=(1920, 1080),
                    slot_index=slot,
                    telemetry=None,
                    classes_names={0: "cow", 1: "vehicle"},
                    num_classes=2,
                    classes=np.array([], dtype=np.int32),
                    boxes_centers=np.empty((0, 2), dtype=np.float32),
                    boxes_corner1=np.empty((0, 2), dtype=np.float32),
                    boxes_corner2=np.empty((0, 2), dtype=np.float32),
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
            assert isinstance(msg, SegmentationSlotMetadata)
            stacked = output_frame_buffer.read(msg.slot_index)
            output_frame_buffer.release(msg.slot_index)
            frames_received += 1
            print(
                f"[Consumer] frame_id={msg.frame_id} "
                f"detections={len(msg.classes)} "
                f"stacked_shape={stacked.shape} "
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
