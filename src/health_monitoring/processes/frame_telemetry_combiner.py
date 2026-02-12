import multiprocessing as mp
import logging
import time
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException
from collections import deque
from src.shared.processes.messages import FrameQueueObject, TelemetryQueueObject, CombinedFrameTelemetryQueueObject
from typing import Optional

from src.shared.processes.constants import *


# ================================================================

logger = logging.getLogger("main.hm_combiner")

if not logger.handlers:  # Avoid duplicate handlers
    video_handler = logging.FileHandler('./logs/hm_combiner.log', mode='w')
    video_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(video_handler)
    logger.setLevel(logging.DEBUG)

# ================================================================


class FrameTelemetryCombiner(mp.Process):
    """
    A multiprocessing Process that combines frames with telemetry data based on timestamps.

    One frame at a time (from oldest, FIFO):
    - Takes frame and its id from frame_queue (multiprocessing Queue)
    - searches in the telemetry_queue (multiprocessing Queue) the telemetry values that best match based
    on the timestamp (max_time_diff_s sets the max time difference allowed).
    Telemetry values are delivered in order of timestamp
    - if no match is found, the matching telemetry value must be set to None in the output object
    - if match is found,removes all the older telemetry values from the queue to free space
    - output is put on a mp.Queue list

    The process can shut-down via a global ErrorEvent being set (hard shutdown),
    or via POISON-PILL (sequential shutdown).
    If the process stops due to error_event, it's not necessary to propagate the poison pill since all processes will
    stop at the same time.

    """

    def __init__(
            self,
            frame_queue: mp.Queue,
            telemetry_queue: mp.Queue,
            output_queue: mp.Queue,
            error_event: mp.Event,
            telemetry_buffer_max_size: int = FRAMETELCOMB_MAX_TELEM_BUFFER_SIZE,
            max_time_diff_s: float = FRAMETELCOMB_MAX_TIME_DIFF,
            queue_get_timeout: float = FRAMETELCOMB_QUEUE_GET_TIMEOUT,
            queue_put_timeout: float = FRAMETELCOMB_QUEUE_PUT_TIMEOUT,
            poison_pill_timeout: float = POISON_PILL_TIMEOUT,
    ):
        """
        Initialize the FrameTelemetryCombiner process.

        Args:
            frame_queue: Queue containing FrameQueueObject instances
            telemetry_queue: Queue containing TelemetryQueueObject instances
            output_queues: List of queues to output CombinedFrameTelemetryQueueObject instances
            error_event: Event to signal the process to stop
            max_time_diff_s: Maximum time difference allowed for matching (seconds)
        """
        super().__init__()

        # mp queues and events
        self.frame_queue = frame_queue
        self.telemetry_queue = telemetry_queue
        self.output_queue = output_queue
        self.error_event = error_event

        # local telemetry buffer for timestamp matching
        self.telemetry_buffer = deque(maxlen=telemetry_buffer_max_size)
        self.max_time_diff_s = max_time_diff_s

        # queue get/put
        self.queue_get_timeout = queue_get_timeout
        self.queue_put_timeout = queue_put_timeout
        self.poison_pill_timeout = poison_pill_timeout

        self.work_finished = mp.Event()

    def _update_telemetry_buffer(self):
        """Collect all available telemetry data from queue into buffer."""
        logger.debug(f"initial telemetry buffer length: {len(self.telemetry_buffer)}")
        while True:
            try:
                telemetry_obj: TelemetryQueueObject = self.telemetry_queue.get_nowait()
                self.telemetry_buffer.append(telemetry_obj)
            except QueueEmptyException:
                logger.debug(
                    "Telemetry queue emptied into the local buffer (if not empty already). "
                    "Stopping fetch for matching to the current frame. "
                    f"Buffer length: {len(self.telemetry_buffer)}"
                )
                break
            except Exception as e:
                logger.debug(
                    f"Unexpected error in telemetry fetch: {e}. "
                    f"Stopping fetch for matching to the current frame"
                )
                break

    def _find_best_match(self, frame_timestamp: float) -> Optional[dict]:
        """
        Find the best matching telemetry for the given frame timestamp.
        Removes all older telemetry values from buffer if match is found.
        Find the best matching telemetry for the given frame timestamp.

        Args:
            frame_timestamp: Timestamp of the frame to match

        Returns:
            Matched telemetry dict or None if no match within time threshold
        """
        if not self.telemetry_buffer:
            logger.warning(f"No telemetry data available for matching at timestamp {frame_timestamp}")
            return None

        best_match = None
        best_diff = float('inf')
        best_idx = -1  # Track index of the telemetry with timestamp closest to that of the frame
        last_too_old_idx = -1  # Track oldest telemetry that is too old and should be removed

        min_valid_timestamp = frame_timestamp - self.max_time_diff_s

        # Find closest telemetry by timestamp
        for idx, telemetry_obj in enumerate(self.telemetry_buffer):
            time_diff = abs(telemetry_obj.timestamp - frame_timestamp)

            if time_diff < best_diff:
                best_diff = time_diff
                best_match = telemetry_obj
                best_idx = idx

            # Track the last telemetry that's too old (before min_valid_timestamp)
            if telemetry_obj.timestamp < min_valid_timestamp:
                last_too_old_idx = idx

            # Since telemetry is ordered by timestamp, if we're past the frame
            # timestamp by too much, we can stop searching
            if telemetry_obj.timestamp > frame_timestamp + self.max_time_diff_s:
                break

        # Remove old telemetry regardless of match success
        if last_too_old_idx >= 0:
            # Remove all telemetry up to and including last_too_old_idx
            for _ in range(last_too_old_idx + 1):
                self.telemetry_buffer.popleft()
            logger.debug(
                f"Removed {last_too_old_idx + 1} telemetry entries "
                f"older than the maximum allowed time difference for matching ({self.max_time_diff_s} seconds)")

            # Adjust best_idx if we removed items before it
            if best_idx >= 0:
                best_idx = best_idx - (last_too_old_idx + 1)

        # Check if best match is within allowed time difference
        if best_diff <= self.max_time_diff_s:
            logger.debug(f"Found telemetry match with time diff: {best_diff:.4f}s")
            # Remove all telemetry older than to the matched one (keep matched one)
            removed_older = 0
            for _ in range(best_idx):
                self.telemetry_buffer.popleft()
                removed_older += 1
            logger.debug(f"Removed {removed_older} telemetries old than the best match from the buffer")
            return best_match.telemetry

        else:
            logger.warning(
                f"No telemetry match found within {self.max_time_diff_s} seconds "
                f"(best diff: {best_diff:.4f}s). "
            )
            return None

    def run(self):
        """Main process loop."""
        logger.info("FrameTelemetryCombiner process started")

        failed_matches = 0
        consecutive_failed_matches = 0
        poison_pill_received = False

        try:

            # Process runs until the stop event is set
            while not self.error_event.is_set():

                try:
                    # Try to get a frame, waiting for a short time if not available immediately
                    frame_obj: FrameQueueObject = self.frame_queue.get(timeout=self.queue_get_timeout)
                except QueueEmptyException:
                    logger.debug("Frame queue is empty, retrying fetch ...")
                    continue

                # if the object found is the poison pill, it must be propagated to following processes via
                # their input queues.
                if isinstance(frame_obj, str) and frame_obj == POISON_PILL:
                    poison_pill_received = True
                    logger.info("Found sentinel value on queue.")
                    try:
                        logger.info("Attempting to put sentinel value on output queue ...")
                        self.output_queue.put(POISON_PILL, timeout=self.poison_pill_timeout)
                        logger.info("Sentinel value has been passed on to the next process.")
                    except Exception as e:
                        logger.error(f"Error propagating Poison Pill: {e}")
                        self.error_event.set()
                        logger.warning(
                            "Error event set: "
                            "force-stop downstream processes since they are unable to receive the poison pill."
                        )
                    break
                    # exit the outer loop and terminate the process execution

                # Collect available telemetry data into buffer
                # stop when all have been collected and the queue is empty, or the local telemetry data buffer is full
                self._update_telemetry_buffer()

                # Find best matching telemetry
                matched_telemetry = self._find_best_match(frame_obj.timestamp)

                if matched_telemetry is None:
                    failed_matches += 1
                    consecutive_failed_matches += 1
                    logger.debug(
                        f"N. Consecutive failed matches: {consecutive_failed_matches}. "
                        f"N. Total failed matches: {failed_matches}. "
                        f"Either the delay between frames and telemetry is too large, or telemetry collection has stopped."
                    )
                else:
                    consecutive_failed_matches = 0

                # Create combined object
                combined_obj = CombinedFrameTelemetryQueueObject(
                    frame_id=frame_obj.frame_id,
                    frame=frame_obj.frame,
                    telemetry=matched_telemetry,
                    timestamp=frame_obj.timestamp,
                    original_wh=frame_obj.original_wh,
                )

                # Put result on downstream queue
                try:
                    self.output_queue.put(combined_obj, timeout=self.queue_put_timeout)
                    logger.debug(
                        f"Put combined frame-telemetry object for frame {combined_obj.frame_id} on output queue"
                    )
                except QueueFullException:
                    logger.error(
                        f"Failed to put combined frame-telemetry for frame {combined_obj.frame_id} on output queue. "
                        "Output queue is full, consumer too slow? "
                        "Discarding frame."
                    )

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in the frame-telemetry combiner process: {e}")
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            # log process conclusion
            logger.info(
                "FrameTelemetryCombiner process stopped gracefully. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()



if __name__ == "__main__":

    import numpy as np
    import random
    from src.shared.processes.consumer import Consumer
    from src.shared.processes.producer import Producer

    VSLOW = 1
    SLOW = 10
    FAST = 50
    REAL = 30
    FREAL = 40

    QUEUE_MAX = 3

    def generate_frame_queue_object():
        ts = time.time()
        return FrameQueueObject(
            frame_id=int(ts*100),
            frame=np.random.randint(0, 256, size=(720, 1280, 3), dtype=np.uint8),
            timestamp=ts,
            original_wh=(1920, 1080),
        )

    def generate_telemetry_object():
        return TelemetryQueueObject(
            telemetry={
                "latitude": random.uniform(-90.0, 90.0),       # degrees
                "longitude": random.uniform(-180.0, 180.0),    # degrees
                "rel_alt": random.uniform(0.0, 100.0),         # meters
                "gb_yaw": random.uniform(0.0, 360.0),          # degrees
            },
            timestamp=time.time()
        )

    frame_queue = mp.Queue(maxsize=QUEUE_MAX)
    telemetry_queue = mp.Queue(maxsize=QUEUE_MAX*10)
    stop_event = mp.Event()
    error_event = mp.Event()

    out1 = mp.Queue(maxsize=QUEUE_MAX)

    stream_reader = Producer(frame_queue, error_event, generate_frame_queue_object, frequency_hz=FREAL)
    stream_reader.stop_with_poison = False
    
    telemetry_reader = Producer(telemetry_queue,error_event, generate_telemetry_object, frequency_hz=SLOW)
    telemetry_reader.stop_with_poison = False   # telemetry reader without giving notice, it ismply stops and puts noting on output queue

    consumer1 = Consumer(out1, error_event, frequency_hz=FAST)

    combiner = FrameTelemetryCombiner(frame_queue, telemetry_queue, out1, error_event)

    print("CONSUMERS STARTED")
    consumer1.start()

    time.sleep(3)

    print("COMBINER STARTED")
    combiner.start()

    time.sleep(3)

    print("READERS STARTED")
    stream_reader.start()
    telemetry_reader.start()

    time.sleep(3)

    # stop without telling via posion pill nor error event
    print("TELEMETRY STOPPED")
    telemetry_reader.stop()

    time.sleep(3)

    # stop without telling via posion pill nor error event
    print("VIDEO STOPPED")
    stream_reader.stop()

    time.sleep(1)

    print("POISON PILL")
    frame_queue.put(POISON_PILL)    # option1
    #print("ERROR EVENT")
    #error_event.set()              # option2

    processes = [stream_reader, telemetry_reader] + [combiner] + [consumer1]

    while True:

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

        time.sleep(0.5)

    print(f"[Main] Granting 5s for all processed to cleanly conclude their processing.")
    time.sleep(5.0)
    # The Sweep: Force everyone to join or die
    for p in processes:
        # If the logic is finished but the process is still 'alive',
        # it is 100% stuck in the queue feeder thread.
        if p.is_alive():
            print(f"[Main] {p.name} is hanging in cleanup. Work Completed: {p.work_finished.is_set()}. Terminating.")
            p.terminate()

        p.join()
        print(f"[Main] {p.name} joined.")
