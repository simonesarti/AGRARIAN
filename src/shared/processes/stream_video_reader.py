import multiprocessing as mp
import cv2
import logging
from time import time, sleep
from queue import Full as QueueFullException
from src.shared.processes.consumer import Consumer
from src.shared.processes.messages import FrameQueueObject
from src.shared.processes.constants import (
    VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S,
    VIDEO_STREAM_READER_RECONNECT_DELAY,
    VIDEO_STREAM_READER_MAX_CONSECUTIVE_CONNECTION_FAILURES,
    VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S,
    VIDEO_STREAM_READER_FRAME_RETRY_DELAY,
    VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES,
    VIDEO_STREAM_READER_BUFFER_SIZE,
    VIDEO_STREAM_READER_EXPECTED_ASPECT_RATIO,
    VIDEO_STREAM_READER_PROCESSING_SHAPE,
    VIDEO_STREAM_READER_QUEUE_PUT_TIMEOUT,
    DOWNSAMPLING_MODE,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
)


# ================================================================

logger = logging.getLogger("main.stream_video_in")

if not logger.handlers:  # Avoid duplicate handlers
    video_handler = logging.FileHandler('./logs/stream_video_in.log', mode='w')
    video_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(video_handler)
    logger.setLevel(logging.DEBUG)

# ================================================================


class StreamVideoReader(mp.Process):
    """
    Reads video stream frames from media server or CDN,
    and pushes them to the frame queue together with id and timestamp.
    The process continually tries to connect to the provided server address.
    The process terminates when it receives an external stop event
    """
    
    def __init__(
            self,
            frame_queue: mp.Queue,
            stop_event: mp.Event,   # stop event used to stop gracefully
            error_event: mp.Event,  # error event used to stop gracefully on processing error
            video_stream_url: str,
            connect_open_timeout_s: float = VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S,
            connect_retry_delay_s: float = VIDEO_STREAM_READER_RECONNECT_DELAY,
            connect_max_consecutive_failures: int = VIDEO_STREAM_READER_MAX_CONSECUTIVE_CONNECTION_FAILURES,
            frame_read_timeout_s: float = VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S,
            frame_read_retry_delay_s: float = VIDEO_STREAM_READER_FRAME_RETRY_DELAY,
            frame_read_max_consecutive_failures: int = VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES,
            buffer_size: int = VIDEO_STREAM_READER_BUFFER_SIZE,
            expected_aspect_ratio: float = VIDEO_STREAM_READER_EXPECTED_ASPECT_RATIO,
            processing_shape: tuple[int, int] = VIDEO_STREAM_READER_PROCESSING_SHAPE,
            queue_out_put_timeout: float = VIDEO_STREAM_READER_QUEUE_PUT_TIMEOUT,
            poison_pill_timeout: float = POISON_PILL_TIMEOUT,
    ):
        
        super().__init__()

        # Shared output queue. Next process will read from this
        self.frame_queue = frame_queue

        # Shared stop event.
        # Allows to stop all processes at the same time
        # (sent by video reading process on correct termination)
        self.stop_event = stop_event

        # Shared error event.
        # Allows to stop all processes at the same time
        # (sent by any process terminating unexpectedly due to error, all other processes should stop)
        self.error_event = error_event

        # video stream URL
        self.video_stream_url = video_stream_url

        # Connection configuration
        self.connect_open_timeout_s = connect_open_timeout_s
        self.connect_retry_delay_s = connect_retry_delay_s
        self.connect_max_consecutive_failures = connect_max_consecutive_failures

        # Frame reading configuration
        self.frame_read_timeout_s = frame_read_timeout_s
        self.frame_read_retry_delay_s = frame_read_retry_delay_s
        self.frame_read_max_consecutive_failures = frame_read_max_consecutive_failures
        self.buffer_size = buffer_size

        # Processing configuration
        self.expected_aspect_ratio = expected_aspect_ratio
        self.processing_shape = processing_shape

        # Output configuration
        self.queue_out_put_timeout = queue_out_put_timeout
        self.poison_pill_timeout = poison_pill_timeout

        self.work_finished = mp.Event()

    def _setup_capture(self) -> cv2.VideoCapture:
        """
        Set up video capture with appropriate settings for video streams.
        """
        cap = cv2.VideoCapture(self.video_stream_url)

        if not cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.connect_open_timeout_s * 1000):
            raise ConnectionError("Failed to set CAP_PROP_OPEN_TIMEOUT_MSEC: blocking behavior on connection is undefined")

        if not cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.frame_read_timeout_s * 1000):
            raise ConnectionError("Failed to set CAP_PROP_READ_TIMEOUT_MSEC: blocking behavior on cap.read() is undefined")

        # CAP_PROP_BUFFERSIZE is a no-op for RTSP/FFmpeg backend; buffer is managed by FFmpeg internally
        # cap.set(cv2.CAP_PROP_BUFFERSIZE, self.buffer_size)

        return cap

    def run(self):
        """Main process loop."""

        # connection failure counters
        total_connection_failures = 0
        consecutive_connection_failures = 0

        # Initialize frame counter
        frame_id = 0

        # read failure counters
        total_read_failures = 0
        consecutive_read_failures = 0

        # placeholder for videoCapture connection
        cap = None

        # try/except wrapper to catch any unforeseen errors
        try:

            # Use a non-blocking check to see if we should stop.
            while not (self.stop_event.is_set() or self.error_event.is_set()):

                # --------------------------------------------------------------
                # setup videocapture
                # - failure treated as connection failure, retry after a delay for a max number of times
                # ensure connection is actually open if setup failed silently
                # - failure treated as connection failure, retry after a delay for a max number of times
                # --------------------------------------------------------------

                # attempt to connect to the video source.
                # A new connection is created at every failure to ensure clean state
                try:
                    logger.info("Setting up VideoCapture Object ...")
                    cap = self._setup_capture()
                    # Manually trigger the exception if the connection isn't actually "live"
                    if cap is None or not cap.isOpened():
                        raise ConnectionError(f"VideoCapture is either not open or not functional for {self.video_stream_url}")
                    logger.info("VideoCapture Object setup complete and stream is open")
                    consecutive_connection_failures = 0
                    consecutive_read_failures = 0
                
                except Exception as e:
                    total_connection_failures += 1
                    consecutive_connection_failures += 1
                    logger.warning(
                        f"Unable to setup videocapture for video source {self.video_stream_url}: {e}. "
                        f"N. Consecutive connection failures: {consecutive_connection_failures} "
                        f"(max: {self.connect_max_consecutive_failures}). "
                        f"N. Total connection failures: {total_connection_failures}. "
                    )

                    # when the max number of connection attempts has not been surpassed yet, retry to connect
                    if consecutive_connection_failures < self.connect_max_consecutive_failures:
                        logger.warning(f"Retrying to setup VideoCapture in {self.connect_retry_delay_s} seconds ...")
                        sleep(self.connect_retry_delay_s)
                        continue

                    # otherwise, stop the application
                    else:
                        logger.warning(
                            f"Max number of consecutive connection attempts to the video stream source surpassed. "
                            f"Video stream not available. Shutting down the application ..."
                        )
                        self.stop_event.set()
                        logger.info("Stop event set")
                        break
                        # after break, jump out of this outer loop to the cleanup code

                # --------------------------------------------------------------
                # connection established
                # --------------------------------------------------------------

                logger.info("Starting video reading loop")
                while cap.isOpened() and not (self.stop_event.is_set() or self.error_event.is_set()):
                    # continue to read frame until the connection is live and no halting events are set
                    # if loop exists due to connection breakdown, the outer loop retries to establish the connection
                    # if loop exits due to halting event, outer loop exists as well,
                    # and the final cleanup code is executed

                    success, frame = cap.read()
                    frame_id += 1
                    # cap.read() is a blocking operation !!!!
                    # still, if nothing has arrived in the timeout time, wait a  bit to retry.

                    # --------- read failure ---------
                    if (not success) or (frame is None) or (frame.size == 0):
                        total_read_failures += 1
                        consecutive_read_failures += 1
                        logger.warning(
                            f"Frame {frame_id} read failed. "
                            f"N. Consecutive read failures: {consecutive_read_failures} "
                            f"(max: {self.frame_read_max_consecutive_failures}). "
                            f"N. Total read failures: {total_read_failures}. "
                        )

                        if consecutive_read_failures < self.frame_read_max_consecutive_failures:
                            logger.warning(f"Attempting new read in {self.frame_read_retry_delay_s} seconds... ")
                            sleep(self.frame_read_retry_delay_s)
                            continue

                        else:
                            logger.error(
                                f"Max number tolerated consecutive frame read failures has been surpassed. "
                                f"Trying to reconnect in {self.connect_retry_delay_s} seconds..."
                            )
                            sleep(self.connect_retry_delay_s)
                            break
                            # breaks out of the inner loop, go back to the outer loop and try to reconnect

                    # --------- read successful, check aspect ration and resize ---------

                    # check that the video frames are in the expected 16/9 aspect ratio
                    # failure here must cause a shutdown of the application
                    # which is only intended to process 16:9 images
                    frame_height, frame_width, _ = frame.shape
                    aspect_ratio = frame_width/frame_height
                    if not abs(aspect_ratio - self.expected_aspect_ratio) < 1.0e-2:     # tolerance: accounts for encoder padding (e.g. 1920x1088)
                        logger.error(
                            f"Application expects frame with aspect ratio (W/H)={self.expected_aspect_ratio} "
                            f"but got frame of size W/H = {frame_width}/{frame_height} = {aspect_ratio}."
                            f"Shutting down the application ..."
                        )
                        self.error_event.set()
                        logger.info("Error event set")
                        break
                        # after break, skip to the end of this inner loop,
                        # enter the outer loop with terminates due to error_event being set.
                        # This causes a jump to the final cleanup code

                    # resize to desired frame size, here (1280, 720) as a compromise between resolution and speed
                    # failure here can simply cause a warning, and risizing the enxt frame will be attempted
                    try:
                        frame = cv2.resize(frame, self.processing_shape, interpolation=DOWNSAMPLING_MODE)
                    except cv2.error:
                        total_read_failures += 1
                        consecutive_read_failures += 1
                        logger.warning(
                            f"Frame {frame_id} resizing failed. "
                            f"N. Consecutive read failures: {consecutive_read_failures} (max: {self.frame_read_max_consecutive_failures}). "
                            f"N. Total read failures: {total_read_failures}. "
                        )

                        if consecutive_read_failures < self.frame_read_max_consecutive_failures:
                            logger.warning(f"Attempting new read in {self.frame_read_retry_delay_s} seconds... ")
                            sleep(self.frame_read_retry_delay_s)
                            continue

                        else:
                            logger.error(
                                f"Max number tolerated consecutive frame read failures has been surpassed. "
                                f"Shutting down the application ..."
                            )
                            self.error_event.set()
                            logger.info("Error event set")
                            break

                    # --------- read successful and checks passed, output queue object ---------

                    # Reset failure counter on successful read and checks passed
                    consecutive_read_failures = 0

                    # Package the frame with its unique frame ID
                    frame_object = FrameQueueObject(
                        frame_id=frame_id,
                        frame=frame,
                        timestamp=time(),
                        original_wh=(frame_width, frame_height)
                    )

                    # Try to put output object in output queue
                    try:
                        self.frame_queue.put(frame_object, timeout=self.queue_out_put_timeout)
                        logger.debug(f"Added frame {frame_id} to queue.")
                    # Catch exception for queue full
                    # no need to sleep since already waited during put timeout
                    except QueueFullException:
                        logger.warning(
                            f"Failed to put frame in queue: Queue is full. "
                            f"Consumer too slow or stopped?. "
                            f"Frame discarded. "
                            f"Trying to read a new frame ..."
                        )

                    # end of successful read of frame
                    # move on to read next frame

            # Propagate termination signal via poison pill if interruption due to stop_event.
            # stop event indicates "clean shutdown" due to input stream termination.
            # In case of error_event, all processes should shut down (cleanly) in the state they currently are,
            # so there is not really a need to propagate the poison pill.
            # If process fails to send the poison pill for clean shutdown, then the error event is set to ensure
            # termination of the application
            if self.stop_event.is_set() and not self.error_event.is_set():
                try:
                    logger.info("Attempting to put sentinel value on output queue ...")
                    self.frame_queue.put(POISON_PILL, timeout=self.poison_pill_timeout)
                    logger.info("Sentinel value has been passed on to the next process.")
                except Exception as e:
                    logger.error(f"Error sending Poison Pill: {e}")
                    self.error_event.set()
                    logger.warning(
                        "Error event set: "
                        "force-stop downstream processes if they are unable to receive the poison pill"
                    )
            else:
                # error event has been set, all processes will stop where they are.
                logger.info("Terminating and Skipping Poison Pill sending. Error Event received.")

        except Exception as e:
            logger.critical(f"An unexpected error happened in Video reader process: {e}")
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:

            #  Final Cleanup

            if cap is not None:
                try:
                    logger.info("Closing VideoCapture object ...")
                    cap.release()
                    logger.info("VideoCapture object closed")
                except Exception as e:
                    logger.error(f"Failed to close the VideoCapture object: {e}")

            # log process conclusion
            logger.info(
                "StreamVideoReader process stopped gracefully. "
                f"Stop event: {self.stop_event.is_set()}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()



if __name__ == "__main__":

    frame_queue = mp.Queue(maxsize=3)
    stop_event = mp.Event()
    error_event = mp.Event()

    video_stream_url = "rtsp://0.0.0.0:8554/annot"

    VSLOW=1
    SLOW=10
    REAL=30
    FAST=50

    stream_reader = StreamVideoReader(frame_queue, stop_event, error_event, video_stream_url)
    consumer = Consumer(frame_queue, error_event, frequency_hz=FAST)

    print("CONSUMERS STARTED")
    consumer.start()

    sleep(2)

    print("VIDEO READER STARTED")
    stream_reader.start()

    event_set = False
    start_time = time()
    block_in = 15.0

    processes = [stream_reader, consumer]

    while True:

        if time()-start_time > block_in and not event_set:
            event_set=True
            #print("PRODUCER STOPPED")
            #producer.stop()
            print("ERROR EVENT SET")
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
