import multiprocessing as mp
import logging
from src.health_monitoring.processes.messages import CombinedAnomalyDetectionResults, TrackingResult, AnomalyInferenceResults
from src.shared.processes.constants import *
from time import time
from queue import Full as QueueFullException
from queue import Empty as QueueEmptyException
from src.health_monitoring.anomaly_detection.anomaly_detection import merge_previous_anomaly_status_current_detections

# ================================================================

logger = logging.getLogger("main.models_alignment")

if not logger.handlers:  # Avoid duplicate handlers
    video_handler = logging.FileHandler('./logs/models_alignment.log', mode='w')
    video_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(video_handler)
    logger.setLevel(logging.DEBUG)


# ================================================================


class ModelsAlignmentWorker(mp.Process):

    def __init__(
            self,
            input_queues: list[mp.Queue],
            result_queue: mp.Queue,
            error_event: mp.Event,
            queue_get_timeout: float = MODELS_QUEUE_GET_TIMEOUT,
            queue_put_timeout: float = MODELS_QUEUE_PUT_TIMEOUT,
            poison_pill_timeout: float = POISON_PILL_TIMEOUT,
    ):
        super().__init__()

        self.input_queues = input_queues
        self.result_queue = result_queue
        self.error_event = error_event

        self.queue_get_timeout = queue_get_timeout / 2
        self.queue_put_timeout = queue_put_timeout
        self.poison_pill_timeout = poison_pill_timeout

        self.work_finished = mp.Event()

    def run(self):

        logger.info("Results aggregation process started.")
        poison_pill_received = False

        most_recent_ids = []
        most_recent_anomalies = []

        try:

            while not self.error_event.is_set():

                iter_start = time()

                # attempt to collect data from first input queue (raw frame)
                try:
                    # raw_tracking_step_results is either a TrackingResult or the poison_pill
                    raw_tracking_step_results: TrackingResult|str = self.input_queues[0].get(timeout=self.queue_get_timeout)
                except QueueEmptyException:
                    raw_tracking_step_results = None
                    logger.debug("Input queue #1 empty")

                # attempt to collect anomaly predictions from second input queue (model predictions)
                try:
                    # anomaly_model_step_results is either a AnomalyResults or the poison_pill
                    anomaly_model_step_results: AnomalyInferenceResults|str = self.input_queues[1].get(timeout=self.queue_get_timeout)
                except QueueEmptyException:
                    anomaly_model_step_results = None
                    logger.debug("Input queue #2 empty")

                # if failed to catch from both queues, retry fetching
                if raw_tracking_step_results is None and anomaly_model_step_results is None:
                    logger.debug("Both queues empty, retrying fetch.... (provider slow or stuck?)")
                    continue

                collected_results = [raw_tracking_step_results, anomaly_model_step_results]

                # check whether any of the results is a poison pill
                # if it is, propagate it and leave the loop
                if any((isinstance(r,str) and r == POISON_PILL) for r in collected_results):
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
                            "Error event set: "
                            "force-stop downstream processes since they are unable to receive the poison pill."
                        )
                    break

                # ------- at least one of the extracted data is valid -------
                
                # check wheter the anomaly inference is valid
                # # if it is, stroe it as the most recent inference vakue, to be repeated until a new one is obtained 
                if anomaly_model_step_results is not None:
                    assert isinstance(anomaly_model_step_results, AnomalyInferenceResults)
                    most_recent_ids =  anomaly_model_step_results.ids
                    most_recent_anomalies = anomaly_model_step_results.status
                    logger.debug("Updated predictions")

                # if only got the prediction but not a new frame to apply the predictions to, go back to try and get a new frame
                if raw_tracking_step_results is None:
                    assert isinstance(raw_tracking_step_results, TrackingResult)
                    logger.debug("Got something, but not a frame. Trying to fetch a new one")
                    continue

                # map the last know prediction onto the currently available entities ids
                # i.e, once an animal has been deemed anomalous, until a new prediction comes telling
                # the entitity has gone back to normal, the entitiy remains marked as anomalous in all new frame
                assert isinstance(raw_tracking_step_results, TrackingResult)
                current_ids=raw_tracking_step_results.objects_ids
                are_anomalous: list[bool] = merge_previous_anomaly_status_current_detections(
                    current_ids=current_ids,
                    previous_ids=most_recent_ids,
                    previous_anomaly_status=most_recent_anomalies,
                )

                # create an alert message if one or more entitites are marked as anomalous
                alert_msg = "anomalous behvaiour detected" if any(are_anomalous) else ""

                aligned_results = CombinedAnomalyDetectionResults(
                    frame=raw_tracking_step_results.frame,
                    frame_id=raw_tracking_step_results.frame_id,
                    classes_names=raw_tracking_step_results.classes_names,
                    num_classes=raw_tracking_step_results.num_classes,
                    classes=raw_tracking_step_results.classes,
                    boxes_corner1=raw_tracking_step_results.boxes_corner1,
                    boxes_corner2=raw_tracking_step_results.boxes_corner2,
                    are_anomalous=are_anomalous,
                    ids=current_ids,
                    alert_msg=alert_msg,
                    timestamp=raw_tracking_step_results.timestamp,
                    original_wh=raw_tracking_step_results.original_wh,
                )

                try:
                    self.result_queue.put(aligned_results, timeout=self.queue_put_timeout)
                    logger.debug(f"frame {raw_tracking_step_results.frame_id} processed in {(time() - iter_start) * 1000:.2f} ms")
                except QueueFullException:
                    logger.warning(
                        "Failed to put aligned model result on output queue. "
                        "Consumer process too slow? "
                        "Discarding aligned frames. "
                    )

        except Exception as e:
            logger.critical(f"Unforeseen critical error in alignment process: {e}")
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application ")

        finally:
            # log process conclusion
            logger.info(
                "Model results alignment process terminated successfully."
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()