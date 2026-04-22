import multiprocessing as mp
import numpy as np
from queue import Empty as QueueEmptyException
import logging
from time import time
from src.danger_detection.output.frames import draw_count
from src.health_monitoring.output.frames import draw_detections
import cv2
from src.health_monitoring.processes.messages import AnomalyDetectionResults
from src.shared.processes.messages import AnnotationResults
from src.shared.processes.constants import *

# ================================================================

logger = logging.getLogger("main.annotation")

if not logger.handlers:  # Avoid duplicate handlers
    video_handler = logging.FileHandler('./logs/annotation.log', mode='w')
    video_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(video_handler)
    logger.setLevel(logging.DEBUG)

# ================================================================


def annotate_frame(
    frame: np.ndarray,
    num_classes: int,
    classes_names: list,
    classes: np.ndarray,
    boxes_corner1: np.ndarray,
    boxes_corner2: np.ndarray,
    are_anomalous: np.ndarray,
    ids: list[str] ,
):

    # annotated_frame = frame.copy()  # copy of the original frame on which to draw
    annotated_frame = frame  # copy of the original frame on which to draw

    # draw detection boxes
    draw_detections(annotated_frame, classes, are_anomalous, ids, boxes_corner1, boxes_corner2)

    # draw animal count
    draw_count(classes, num_classes, classes_names, annotated_frame)

    return annotated_frame


class AnnotationWorker(mp.Process):

    def __init__(
            self,
            input_queue: mp.Queue,
            video_stream_queue: mp.Queue,
            alerts_stream_queue: mp.Queue,
            error_event: mp.Event,
            alerts_cooldown_seconds: int|float,
            queue_get_timeout: float = ANNOTATION_QUEUE_GET_TIMEOUT,
            queue_put_timeout: float = ANNOTATION_QUEUE_PUT_TIMEOUT,
            max_put_alert_consecutive_failures: int = ANNOTATION_MAX_PUT_ALERT_CONSECUTIVE_FAILURES,
            max_put_video_consecutive_failures: int = ANNOTATION_MAX_PUT_VIDEO_CONSECUTIVE_FAILURES,
            poison_pill_timeout: float = POISON_PILL_TIMEOUT,
    ):
        super().__init__()

        self.input_queue = input_queue

        self.video_stream_queue = video_stream_queue
        self.alerts_stream_queue = alerts_stream_queue
        self.stream_queues = [
            self.video_stream_queue,
            self.alerts_stream_queue,
        ]

        self.error_event = error_event

        self.cooldown = alerts_cooldown_seconds

        self.queue_get_timeout = queue_get_timeout
        self.queue_put_timeout = queue_put_timeout / 2  # split total between 2 queues

        self.max_put_alert_consecutive_failures = max_put_alert_consecutive_failures
        self.max_put_video_consecutive_failures = max_put_video_consecutive_failures

        self.poison_pill_timeout = poison_pill_timeout

        self.work_finished = mp.Event()

    def run(self):

        logger.info("Annotation process started.")
        poison_pill_received = False

        put_alert_failures = 0
        put_alert_consecutive_failures = 0

        put_video_failures = 0
        put_video_consecutive_failures = 0

        last_alert_received_timestamp = -np.inf

        logger.info("Running...")

        try:

            while not self.error_event.is_set():

                iter_start_time = time()

                # ==========================================
                # =============== INPUT FETCHING ===========
                # ==========================================

                fetch_start_time = time()

                try:
                    # previous_step_results is either a AnomalyDetectionResults or the poison_pill
                    previous_step_results: AnomalyDetectionResults|str = self.input_queue.get(timeout=self.queue_get_timeout)
                except QueueEmptyException:
                    logger.debug("Input queue empty, retrying data fetch ... (previous process too slow or stuck?)")
                    continue    # Go back and try to read again from queue, also check the error event condition

                fetch_time = time() - fetch_start_time

                if isinstance(previous_step_results, str) and previous_step_results == POISON_PILL:
                    poison_pill_received = True
                    logger.info("Found sentinel value on queue.")
                    try:
                        logger.info("Attempting to put sentinel value on output queues ...")
                        for pidx, out_queue in enumerate(self.stream_queues, 1):
                            out_queue.put(POISON_PILL, timeout=self.poison_pill_timeout)
                            logger.info(f"Sentinel value has been passed on to downstream process #{pidx}.")
                    except Exception as e:
                        logger.error(f"Error propagating Poison Pill to one or more of the output queues: {e}")
                        self.error_event.set()
                        logger.warning(
                            "Error event set: force-stop application since downstream processes "
                            "are unable to receive the poison pill."
                        )
                    break

                # ==========================================
                # =============== DATA PROCESSING ==========
                # ==========================================
                assert isinstance(previous_step_results, AnomalyDetectionResults)

                processing_start_time = time()

                annotated_frame = annotate_frame(
                        frame=previous_step_results.frame,
                        num_classes=previous_step_results.num_classes,
                        classes_names=previous_step_results.classes_names,
                        classes=previous_step_results.classes,
                        boxes_corner1=previous_step_results.boxes_corner1,
                        boxes_corner2=previous_step_results.boxes_corner2,
                        are_anomalous=previous_step_results.are_anomalous,
                        ids=previous_step_results.ids,
                )
                
                annotated_frame = cv2.resize(
                    src=annotated_frame,
                    dsize=previous_step_results.original_wh,    # (w,h)
                    interpolation=UPSAMPLING_MODE,
                )

                result = AnnotationResults(
                    frame_id=previous_step_results.frame_id,
                    annotated_frame=annotated_frame,
                    alert_msg=previous_step_results.alert_msg,   # str
                    timestamp=previous_step_results.timestamp
                )

                # Check if alert should be sent
                since_last_alert = (previous_step_results.timestamp - last_alert_received_timestamp)
                cooldown_has_passed = since_last_alert > self.cooldown
                alert_exist = len(previous_step_results.alert_msg) > 0

                send_alert = cooldown_has_passed and alert_exist
                if send_alert:
                    last_alert_received_timestamp = previous_step_results.timestamp

                processing_time = time() - processing_start_time

                # ==========================================
                # =============== RESULTS PROPAGATION ======
                # ==========================================

                # if processing concludes successfully:
                # ==> pass the result to the downstream queues
                propagate_start_time = time()

                # to the alert stream queue, pass complete annotation object only if an alert should be sent (flag)
                try:
                    if send_alert:
                        self.alerts_stream_queue.put(result, timeout=self.queue_put_timeout)
                        put_alert_consecutive_failures = 0
                        logger.debug(f"Enqueued alert for notifying user. Since last alerts: {since_last_alert:.2f} seconds")
                except Exception as e:
                    put_alert_failures += 1
                    put_alert_consecutive_failures += 1
                    logger.error(f"Failed to send alert to next process: {e}")
                    if put_alert_consecutive_failures < self.max_put_alert_consecutive_failures:
                        logger.warning(
                            f"Consecutive failures: {put_alert_consecutive_failures} "
                            f"(max {self.max_put_alert_consecutive_failures}). "
                            f"Total failures: {put_alert_failures}. "
                            f"Attempting to send the next alert .."
                        )
                        continue
                    else:
                        logger.error("Max consecutive alert sending failures threshold passed")
                        self.error_event.set()
                        logger.warning("Error event set: force-stopping the application")
                        break

                # to the video stream queue, pass all frames
                # prefer to push the frame without waiting, drop the frame if necessary
                try:
                    self.video_stream_queue.put(annotated_frame, timeout=self.queue_put_timeout)
                    put_video_consecutive_failures = 0
                    logger.debug("Enqueued alert for video streaming")
                except Exception as e:
                    put_video_failures += 1
                    put_video_consecutive_failures += 1
                    logger.error(f"Failed to send annotated frame to next process: {e}")
                    if put_video_consecutive_failures < self.max_put_video_consecutive_failures:
                        logger.warning(
                            f"Consecutive failures: {put_video_consecutive_failures} "
                            f"(max {self.max_put_video_consecutive_failures}). "
                            f"Total failures: {put_video_failures}. "
                            f"Attempting to send the annotated frame .."
                        )
                        continue
                    else:
                        logger.error("Max consecutive frame sending failures threshold passed")
                        self.error_event.set()
                        logger.warning("Error event set: force-stopping the application")
                        break

                propagate_time = time() - propagate_start_time
                iter_time = time() - iter_start_time

                # monitor performance
                logger.debug(
                    f"frame {result.frame_id} processed in {iter_time * 1000:.3f} ms, "
                    f"of which --> "
                    f"FETCH: {fetch_time * 1000:.3f} ms, "
                    f"PROCESS: {processing_time * 1000:.3f} ms, "
                    f"PROPAGATE: {propagate_time * 1000:.3f} ms."
                )

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in annotation process: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            # log process conclusion
            logger.info(
                "Danger annotation process terminated successfully. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()


if __name__ == "__main__":

    import numpy as np
    import random
    from src.shared.processes.consumer import Consumer
    from src.shared.processes.producer import Producer
    from time import sleep, time, perf_counter
    from src.danger_detection.processes.messages import AnomalyDetectionResults

    VSLOW = 1
    SLOW = 10
    FAST = 50
    REAL = 30
    FREAL = 40

    QUEUE_MAX = 3

    stop_with_poison_pill = True
    stop_after = 20.0


    def generate_queue_object():
        ts=time()
        num_animals = random.randint(0,10)
        boxes_centers = np.array([(random.randint(0,639), random.randint(0,479)) for _ in range(num_animals)])
        boxes_corner1= boxes_centers - 15
        boxes_corner2 = boxes_centers + 15

        return AnomalyDetectionResults(
            frame_id=int(ts*100),
            frame=np.zeros((720,1080,3), dtype=np.uint8),
            num_classes=2,
            classes_names=["goat", "sheep"],
            classes=[random.randint(0,1) for _ in range(num_animals)],
            boxes_corner1=boxes_corner1,
            boxes_corner2=boxes_corner2,
            danger_types="anomaly" if random.random()>0.5 else "",
            timestamp=ts,
            original_wh=(1920,1080),
        )

    queue_in = mp.Queue(maxsize=QUEUE_MAX)
    
    video_queue_out = mp.Queue(maxsize=QUEUE_MAX)
    alert_queue_out = mp.Queue(maxsize=QUEUE_MAX)

    stop_event = mp.Event()
    error_event = mp.Event()

    producer = Producer(queue_in, error_event, generate_queue_object, frequency_hz=FAST)
    
    worker = AnnotationWorker(
        input_queue=queue_in,
        video_stream_queue=video_queue_out,
        alerts_stream_queue=alert_queue_out,
        error_event=error_event,
        alerts_cooldown_seconds=2.0,
    )
    
    consumer1 = Consumer(video_queue_out, error_event, frequency_hz=FAST)
    consumer2 = Consumer(alert_queue_out, error_event, frequency_hz=FAST)


    print("CONSUMERS STARTED")
    consumer1.start()
    consumer2.start()

    sleep(1)

    print("WORKER STARTED")
    worker.start()

    sleep(1)

    print("PRODUCER STARTED")
    producer.start()

    sleep(1)

    start_at = time()
    stop_at = start_at + stop_after

    processes = [producer, worker, consumer1, consumer2]

    signal_set = False

    while True:
        
        if time() > stop_at and not signal_set:
            signal_set = True
            if stop_with_poison_pill:
                print("POISON PILL")
                producer.stop()
            else:
                print("ERROR EVENT")
                error_event.set()

        # Check if everyone has finished their logic
        all_finished = all(p.work_finished.is_set() for p in processes)

        # Check if an error occurred anywhere
        error_occurred = error_event.is_set()

        if all_finished or error_occurred:
            if error_occurred:
                print("[Main] Error detected. Terminating chain.")
            else:
                print("[Main] All processes finished logic. Cleaning up.")
            break

        sleep(0.5)

    print(f"[Main] Granting 5s for all processed to cleanly conclude their processing.")
    sleep(5.0)
    # The Sweep: Force everyone to join or die
    for p in processes:
        # If the logic is finished but the process is still 'alive',
        # it is 100% stuck in the queue feeder thread.
        if p.is_alive():
            print(f"[Main] {p.name} is hanging in cleanup. Work Completed: {p.work_finished.is_set()}. Terminating.")
            p.terminate()

        p.join()
        print(f"[Main] {p.name} joined.")

