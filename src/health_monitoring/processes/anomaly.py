import multiprocessing as mp
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException

from ultralytics import YOLO

from src.health_monitoring.processes.messages import TrackingResult, AnomalyInferenceResults
from src.shared.processes.constants import *
from time import time, sleep
import logging

from src.health_monitoring.anomaly_detection.running_history import EntityRunningHistory, CameraRunningHistory


# ================================================================

logger = logging.getLogger("main.anomaly_detector")

if not logger.handlers:  # Avoid duplicate handlers
    video_handler = logging.FileHandler('./logs/anomaly_detector.log', mode='w')
    video_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(video_handler)
    logger.setLevel(logging.DEBUG)
# ================================================================


class AnomalyDetectorWrapper:

    def __init__(self, predict_args):
        
        self.predict_args = predict_args
        # self.anomaly_detector = self._instantiate()

    # TODO: IMPLEMENT
    def _instantiate(self):
        pass
    
    # TODO: IMPLEMENT
    def _make_data(self):
        pass

    # TODO: IMPLEMENT
    def predict(self, window):
        data = self._make_data(window)
        labels = self.model(data)
        return data.ids, labels


class AnomalyDetectionWorker(mp.Process):
    """
    AnomalyDetectionWorker is a standalone process that:
    - Instantiates the anomaly dection model once during initialization
    - Stores 'anomaly_detection_args' for consistent use
    - Processes incoming tracks and sends back results (ids: list[int] and associated anomaly status:list[bool])
    - Shuts down when it receives a poison pill, forwarding the termination signal to the next process in the sequence
    """

    def __init__(
            self,
            input_queue: mp.Queue,
            result_queue: mp.Queue,
            error_event: mp.Event,
            anomaly_detection_args: dict,
            queue_get_timeout: float = MODELS_QUEUE_GET_TIMEOUT,
            queue_put_timeout: float = MODELS_QUEUE_PUT_TIMEOUT,
            poison_pill_timeout: float = POISON_PILL_TIMEOUT,
    ):
        super().__init__()

        self.input_queue = input_queue
        self.result_queue = result_queue

        self.error_event = error_event

        self.anomaly_detection_args = anomaly_detection_args

        self.queue_get_timeout = queue_get_timeout
        self.queue_put_timeout = queue_put_timeout
        self.poison_pill_timeout = poison_pill_timeout

        self.work_finished = mp.Event()

    def run(self):
        """
        Main loop of the process: initializes the anomaly detector and processes frames.
        """
        
        logger.info("Animal anomaly detection process started.")
        poison_pill_received = False

        try:

            # instantiate the anomaly tracking model
            anomaly_detector = AnomalyDetectorWrapper(predict_args=self.anomaly_detection_args)
            logger.info("Animal anomaly detection model loaded.")

            while not self.error_event.is_set():

                # ==========================================
                #  DATA FETCHING
                # ==========================================

                iter_start = time()

                try:
                    # tracking_object is either a TrackingResult or the POISON_PILL
                    tracking_object: TrackingResult | str = self.input_queue.get(timeout=self.queue_get_timeout)
                except QueueEmptyException:
                    logger.debug(f"Input queue timed out. Upstream producer may be stalled. Retrying...")
                    continue  # Go back and try to get again

                if isinstance(tracking_object, str) and tracking_object == POISON_PILL:
                    poison_pill_received = True
                    logger.info("Found sentinel value on queue.")
                    try:
                        logger.info("Attempting to put sentinel value on output queue ...")
                        self.result_queue.put(POISON_PILL, timeout=self.poison_pill_timeout)
                        logger.info("Sentinel value has been passed on to the next process.")
                    except Exception as e:
                        logger.error(f"Error propagating Poison Pill: {e}")
                        self.error_event.set()
                        logger.warning(
                            "Error event set: force-stop application since downstream processes "
                            "are unable to receive the poison pill."
                        )
                    break

                get_time = time() - iter_start

                # ==========================================
                #  PROCESSING
                # ==========================================

                assert isinstance(tracking_object, TrackingResult)

                # Perform tracking using stored arguments
                predict_start = time()
                entities_ids, entities_status = anomaly_detector.predict() 
                predict_time = time() - predict_start

                result = AnomalyInferenceResults(
                    ids=entities_ids,
                    status=entities_status,
                )

                # ==========================================
                #  RESULT PROPAGATION
                # ==========================================

                # put result in output queue
                append_start = time()
                try:
                    self.result_queue.put(result, timeout=self.queue_put_timeout)
                    logger.debug("Put anomaly detection results on output queue")
                except QueueFullException:
                    logger.error(
                        f"Failed to put tracking results on output queue: queue is full. "
                        f"Consumer too slow or stuck?. "
                        f"Skipping tracking results. "
                    )
                append_time = time() - append_start

                iter_time = time()-iter_start

                logger.debug(
                    f"frame {tracking_object.frame_id} processed in {iter_time * 1000:.2f} ms, "
                    f"of which --> "
                    f"GET: {get_time * 1000:.2f} ms, "
                    f"PREDICT: {predict_time * 1000:.2f} ms, "
                    f"PROPAGATE: {append_time * 1000:.2f} ms."
                )
                # iteration completed correctly, move on to process next frame

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in the animal anomaly detection process: {e}")
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:

            logger.info(
                "Animal anomaly detection process terminated successfully. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()



if __name__ == "__main__":

    import numpy as np
    from src.shared.processes.consumer import Consumer
    from src.shared.processes.producer import Producer
    from src.configs.utils import read_yaml_config

    VSLOW = 1
    SLOW = 10
    FAST = 50
    REAL = 30

    CONSUMER_QUEUE_MAX = 10

    tracking_args = read_yaml_config("configs/health_monitoring/tracker.yaml")

    def generate_frame_telemetry_queue_object():
        ts = time()
        return CombinedFrameTelemetryQueueObject(
            frame_id=int(ts*100),
            frame=np.random.randint(0, 256, size=(720, 1280, 3), dtype=np.uint8),
            telemetry=None,
            timestamp=ts,
            original_wh=(1920, 1080),
        )

    frame_telemetry_queue = mp.Queue()
    stop_event = mp.Event()
    error_event = mp.Event()

    out_queue = mp.Queue(maxsize=CONSUMER_QUEUE_MAX)

    producer = Producer(frame_telemetry_queue, error_event, generate_frame_telemetry_queue_object, frequency_hz=SLOW)
    consumer = Consumer(out_queue, error_event, frequency_hz=SLOW)

    tracker = TrackerWorker(frame_telemetry_queue, out_queue, error_event, tracking_args)

    print("CONSUMERS STARTED")
    consumer.start()

    sleep(3)

    print("TRACKER STARTED")
    tracker.start()

    sleep(3)

    print("PRODUCER STARTED")
    producer.start()

    sleep(5)

    #print("PRODUCER STOPPED")
    #producer.stop()
    print("ERROR EVENT SET")
    error_event.set()

    sleep(5)

    producer.join(timeout=5)
    print("producer joined")

    tracker.join()
    print("tracker joined")

    consumer.join()
    print("consumer joined")
