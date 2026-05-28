import multiprocessing as mp
import multiprocessing.synchronize
import logging
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException
from time import time, sleep

from pydantic import BaseModel, Field, PositiveFloat, field_validator
from ultralytics import YOLO

from src.danger_detection.detection.detection import postprocess_detection_results
from src.danger_detection.processes.messages import DetectionSlotMetadata
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.messages import CombinedSlotMetadata
from src.shared.processes.constants import (
    PIPELINE_QUEUE_TIMEOUT,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
)


# ================================================================

logger = logging.getLogger("main.danger_detector")

if not logger.handlers:  # Avoid duplicate handlers
    _handler = logging.FileHandler('./logs/animals_detection.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

# ================================================================


class DetectionWorkerConfig(BaseModel):
    """Configuration for DetectionWorker."""

    model_checkpoint: str
    predict_args: dict = Field(default_factory=dict)   # YOLO predict kwargs (conf, iou, device, etc.)

    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT

    @field_validator('model_checkpoint')
    @classmethod
    def must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("model_checkpoint must not be empty")
        return v


class DetectorWrapper:

    def __init__(self, model, predict_args):
        self.detector = model
        self.predict_args = predict_args

    def predict(self, frame):
        logger.info(f"Predict args: {self.predict_args}")
        detection_results = self.detector.predict(source=frame, **self.predict_args)
        return postprocess_detection_results(detection_results)


class DetectionWorker(mp.Process):
    """
    Animal detection process in the danger detection pipeline.

    Reads frames from the upstream FrameBuffer, runs YOLO inference, and passes
    the frame unchanged to the downstream FrameBuffer. Detection results (bounding
    boxes, class IDs) are small enough to travel in the lightweight metadata queue
    alongside the slot index.

    The frame is not modified by detection; it is forwarded so that the segmentation
    process can operate on it in the next chain step.

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
            config: DetectionWorkerConfig,
    ):
        super().__init__()

        # Input: frames arrive as lightweight metadata referencing input shared memory slots
        self.input_meta_queue = input_meta_queue
        self.input_frame_buffer = input_frame_buffer

        # Output: frame forwarded to next hop's shared memory, detection results in metadata
        self.output_meta_queue = output_meta_queue
        self.output_frame_buffer = output_frame_buffer

        self.error_event = error_event
        self.config = config

        self.work_finished = mp.Event()

    def run(self):
        """
        Main loop of the process: instantiates the detector once, then processes frames.
        """
        logger.info("Animal detection process started.")
        poison_pill_received = False

        try:

            # Instantiate the detection model inside run() so that GPU context and model
            # weights are loaded in the child process, not inherited from the parent.
            model = YOLO(self.config.model_checkpoint, task="detect")
            detector = DetectorWrapper(model=model, predict_args=self.config.predict_args)
            logger.info("Animal detection model loaded.")

            # model.names is a {class_id: class_name} dict — fixed for the lifetime of this process
            classes_names = model.names
            num_classes = len(classes_names)

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

                assert isinstance(meta, CombinedSlotMetadata)

                get_start = time()

                # ---- read frame from input shared memory and immediately release the slot ----
                # release() is called right after read() so that the upstream process
                # can reuse the slot as quickly as possible
                frame = self.input_frame_buffer.read(meta.slot_index)
                self.input_frame_buffer.release(meta.slot_index)

                # ---- run YOLO inference ----
                predict_start = time()
                classes, boxes_centers, boxes_corner1, boxes_corner2 = detector.predict(frame)

                # ---- acquire an output slot and forward the frame to output shared memory ----
                out_slot = self.output_frame_buffer.acquire()
                if out_slot is None:
                    # No free output slot — the downstream process is too slow.
                    # Drop this frame so the next one can be written when a slot is freed.
                    logger.warning(
                        f"No free slot in output frame buffer. "
                        f"Frame {meta.frame_id} discarded. Consumer too slow?"
                    )
                    continue

                # The frame is forwarded unchanged; detection results travel in the metadata queue.
                # Detection box arrays are small (O(N_detections * few bytes)) and are safe to
                # pickle in the queue alongside the lightweight metadata.
                self.output_frame_buffer.write(out_slot, frame)

                # ---- put combined metadata on the output queue ----
                out_meta = DetectionSlotMetadata(
                    frame_id=meta.frame_id,
                    timestamp=meta.timestamp,
                    original_wh=meta.original_wh,
                    slot_index=out_slot,
                    telemetry=meta.telemetry,
                    classes_names=classes_names,
                    num_classes=num_classes,
                    classes=classes,
                    boxes_centers=boxes_centers,
                    boxes_corner1=boxes_corner1,
                    boxes_corner2=boxes_corner2,
                )

                # no need to sleep on failure since we already waited during the put timeout
                append_start = time()
                try:
                    self.output_meta_queue.put(out_meta, timeout=self.config.queue_timeout)
                    logger.debug(
                        f"Frame {meta.frame_id} → output slot {out_slot}, "
                        f"detection results queued."
                    )
                except QueueFullException:
                    # Return the output slot to the free pool since no metadata was queued —
                    # the consumer will never know to release it otherwise
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
            # In case of error_event, all processes stop where they are, so no pill is needed.
            # If sending the poison pill fails, set the error event to force-stop downstream.
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
                # error event has been set: all processes will stop where they are
                logger.info("Terminating and skipping Poison Pill sending. Error event is set.")

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in the animal detection process: {e}")
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:

            # Detach from shared memory in this process.
            # The parent is responsible for calling unlink() after all processes have finished.
            self.input_frame_buffer.close()
            self.output_frame_buffer.close()

            logger.info(
                "Animal detection process terminated successfully. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()


if __name__ == "__main__":

    import numpy as np
    import threading
    from queue import Empty as QueueEmptyException

    FRAME_SHAPE = (720, 1280, 3)  # (H, W, C) — numpy convention
    N_SLOTS = 3
    PRODUCER_FPS = 30             # fixed: simulates real stream cadence

    # Consumer read frequency — set manually to test slow/medium/fast consumer behaviour.
    # slow=10, medium=30, fast=50  (fps)
    CONSUMER_FPS = 20

    # Set to True to put a poison pill on the input queue after 10 s, testing clean shutdown.
    TRIGGER_POISON_PILL_AFTER_10S = False

    # Set to True to trigger error_event after 10 s, testing clean error-path shutdown.
    TRIGGER_ERROR_AFTER_10S = True

    _PRODUCER_FRAME_INTERVAL = 1.0 / PRODUCER_FPS
    _CONSUMER_FRAME_INTERVAL = 1.0 / CONSUMER_FPS

    error_event = mp.Event()

    input_meta_queue = mp.Queue(maxsize=N_SLOTS)
    input_frame_buffer = FrameBuffer(frame_shape=FRAME_SHAPE, n_slots=N_SLOTS)

    output_meta_queue = mp.Queue(maxsize=N_SLOTS)
    output_frame_buffer = FrameBuffer(frame_shape=FRAME_SHAPE, n_slots=N_SLOTS)

    config = DetectionWorkerConfig(
        model_checkpoint="checkpoints/detection_1280_720_yolo11m.pt",
    )

    worker = DetectionWorker(
        input_meta_queue=input_meta_queue,
        input_frame_buffer=input_frame_buffer,
        output_meta_queue=output_meta_queue,
        output_frame_buffer=output_frame_buffer,
        error_event=error_event,
        config=config,
    )

    def producer_loop():
        """Push fake frames continuously into the input shared memory buffer."""
        frame_id = 0
        while not error_event.is_set():
            iter_start = time()
            slot = input_frame_buffer.acquire()
            if slot is not None:
                frame = np.random.randint(0, 256, FRAME_SHAPE, dtype=np.uint8)
                input_frame_buffer.write(slot, frame)
                meta = CombinedSlotMetadata(
                    frame_id=frame_id,
                    timestamp=time(),
                    original_wh=(1920, 1080),
                    slot_index=slot,
                    telemetry=None,
                )
                try:
                    input_meta_queue.put(meta, timeout=1.0)
                except Exception:
                    input_frame_buffer.release(slot)
            else:
                print(f"[Producer] No free input slot — frame {frame_id} dropped.")
            frame_id += 1
            elapsed = time() - iter_start
            remaining = _PRODUCER_FRAME_INTERVAL - elapsed
            if remaining > 0:
                sleep(remaining)
        input_meta_queue.put(POISON_PILL)
        print("[Producer] Stopped.")

    def consumer_loop():
        """Drain the output queue and release output slots."""
        frames_received = 0
        start = time()
        while True:
            iter_start = time()
            try:
                msg = output_meta_queue.get(timeout=5.0)
            except QueueEmptyException:
                if error_event.is_set():
                    break
                print("[Consumer] Queue empty, retrying ...")
                continue
            if isinstance(msg, str) and msg == POISON_PILL:
                output_meta_queue.put(POISON_PILL)  # re-queue for any additional downstream consumers
                print(f"[Consumer] Poison pill received. {frames_received} frames processed.")
                break
            if error_event.is_set():
                break
            assert isinstance(msg, DetectionSlotMetadata)
            output_frame_buffer.release(msg.slot_index)
            frames_received += 1
            elapsed = time() - start
            print(
                f"[Consumer] frame_id={msg.frame_id} "
                f"detections={len(msg.classes)} "
                f"slot={msg.slot_index} "
                f"fps={frames_received / elapsed:.1f}"
            )
            elapsed_iter = time() - iter_start
            remaining = _CONSUMER_FRAME_INTERVAL - elapsed_iter
            if remaining > 0:
                sleep(remaining)

    def poison_pill_trigger():
        sleep(10)
        print("[PoisonPillTrigger] Putting poison pill on input queue after 10 s.")
        input_meta_queue.put(POISON_PILL)

    def error_trigger():
        sleep(10)
        print("[ErrorTrigger] Setting error event after 10 s.")
        error_event.set()

    prod_thread = threading.Thread(target=producer_loop, daemon=True)
    cons_thread = threading.Thread(target=consumer_loop, daemon=True)

    print("[Main] Starting worker ...")
    worker.start()
    sleep(0.5)  # let the worker process fully start before feeding it

    print("[Main] Starting consumer ...")
    cons_thread.start()

    print("[Main] Starting producer ...")
    prod_thread.start()

    if TRIGGER_POISON_PILL_AFTER_10S:
        threading.Thread(target=poison_pill_trigger, daemon=True).start()

    if TRIGGER_ERROR_AFTER_10S:
        threading.Thread(target=error_trigger, daemon=True).start()

    worker.join()
    prod_thread.join(timeout=5.0)
    cons_thread.join(timeout=5.0)

    input_frame_buffer.unlink()
    output_frame_buffer.unlink()
    print("[Main] Done.")
