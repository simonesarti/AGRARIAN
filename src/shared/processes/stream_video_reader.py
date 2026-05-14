import multiprocessing as mp
import multiprocessing.synchronize
import cv2
import logging
from time import time, sleep
from queue import Full as QueueFullException

from pydantic import BaseModel, PositiveFloat, PositiveInt, field_validator

from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.messages import FrameSlotMetadata
from src.shared.processes.constants import (
    VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S,
    VIDEO_STREAM_READER_RECONNECT_DELAY,
    VIDEO_STREAM_READER_MAX_CONSECUTIVE_CONNECTION_FAILURES,
    VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S,
    VIDEO_STREAM_READER_FRAME_RETRY_DELAY,
    VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES,
    VIDEO_STREAM_READER_EXPECTED_ASPECT_RATIO,
    VIDEO_STREAM_READER_PROCESSING_SHAPE,
    PIPELINE_QUEUE_TIMEOUT,
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


class StreamVideoReaderConfig(BaseModel):
    """Configuration for StreamVideoReader."""

    video_stream_url: str

    # Connection
    connect_open_timeout_s: PositiveFloat = VIDEO_STREAM_READER_CONNECTION_OPEN_TIMEOUT_S
    connect_retry_delay_s: PositiveFloat = VIDEO_STREAM_READER_RECONNECT_DELAY
    connect_max_consecutive_failures: PositiveInt = VIDEO_STREAM_READER_MAX_CONSECUTIVE_CONNECTION_FAILURES

    # Frame reading
    frame_read_timeout_s: PositiveFloat = VIDEO_STREAM_READER_FRAME_READ_TIMEOUT_S
    frame_read_retry_delay_s: PositiveFloat = VIDEO_STREAM_READER_FRAME_RETRY_DELAY
    frame_read_max_consecutive_failures: PositiveInt = VIDEO_STREAM_READER_FRAME_MAX_CONSECUTIVE_FAILURES

    # Processing
    expected_aspect_ratio: PositiveFloat = VIDEO_STREAM_READER_EXPECTED_ASPECT_RATIO
    processing_shape: tuple[int, int] = VIDEO_STREAM_READER_PROCESSING_SHAPE  # (W, H)

    # Output
    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT

    @field_validator('processing_shape')
    @classmethod
    def must_be_valid_shape(cls, v: tuple) -> tuple:
        if len(v) != 2 or v[0] <= 0 or v[1] <= 0:
            raise ValueError(f"processing_shape must be a (W, H) tuple of positive ints, got {v}")
        return v


class StreamVideoReader(mp.Process):
    """
    First process in the pipeline. Reads frames from a media server via RTSP/RTMP,
    resizes them to the configured processing shape, writes each frame zero-copy into
    a shared FrameBuffer slot, and places a lightweight FrameSlotMetadata on the output
    metadata queue for the next process to consume.

    Termination:
    - Clean shutdown: when the stream ends or connection retries are exhausted, a
      POISON_PILL is placed on the metadata queue so downstream processes flush and
      stop in order.
    - Error shutdown: if error_event is set by any process, this process stops
      immediately without flushing.

    Frame drop policy: if no buffer slot is free (consumer too slow) or the metadata
    queue is full, the current frame is discarded so the consumer always receives the
    most recent available frame.
    """

    def __init__(
            self,
            output_meta_queue: mp.Queue,
            output_frame_buffer: FrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: StreamVideoReaderConfig,
    ):
        super().__init__()

        self.output_meta_queue = output_meta_queue
        self.output_frame_buffer = output_frame_buffer
        self.error_event = error_event
        self.config = config

        self.work_finished = mp.Event()

    def _setup_capture(self) -> cv2.VideoCapture:
        """
        Set up video capture with appropriate settings for video streams.
        """
        cap = cv2.VideoCapture(self.config.video_stream_url)

        if not cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.config.connect_open_timeout_s * 1000):
            raise ConnectionError(
                "Failed to set CAP_PROP_OPEN_TIMEOUT_MSEC: "
                "blocking behavior on connection is undefined"
            )

        if not cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.config.frame_read_timeout_s * 1000):
            raise ConnectionError(
                "Failed to set CAP_PROP_READ_TIMEOUT_MSEC: "
                "blocking behavior on cap.read() is undefined"
            )

        # CAP_PROP_BUFFERSIZE is a no-op for RTSP/FFmpeg backend; buffer is managed by FFmpeg internally
        # cap.set(cv2.CAP_PROP_BUFFERSIZE, ...)

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
            while not self.error_event.is_set():

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
                        raise ConnectionError(
                            f"VideoCapture is either not open or not functional "
                            f"for {self.config.video_stream_url}"
                        )
                    logger.info("VideoCapture Object setup complete and stream is open")
                    consecutive_connection_failures = 0
                    consecutive_read_failures = 0

                except Exception as e:
                    total_connection_failures += 1
                    consecutive_connection_failures += 1
                    logger.warning(
                        f"Unable to setup videocapture for {self.config.video_stream_url}: {e}. "
                        f"N. Consecutive connection failures: {consecutive_connection_failures} "
                        f"(max: {self.config.connect_max_consecutive_failures}). "
                        f"N. Total connection failures: {total_connection_failures}."
                    )

                    # when the max number of connection attempts has not been surpassed yet, retry to connect
                    if consecutive_connection_failures < self.config.connect_max_consecutive_failures:
                        logger.warning(
                            f"Retrying to setup VideoCapture "
                            f"in {self.config.connect_retry_delay_s} seconds ..."
                        )
                        sleep(self.config.connect_retry_delay_s)
                        continue

                    # otherwise, initiate clean shutdown via poison pill
                    else:
                        logger.error(
                            "Max consecutive connection attempts surpassed. "
                            "Video stream not available. Shutting down the pipeline ..."
                        )
                        break
                        # after break, jump out of this outer loop to the poison pill / cleanup code

                # --------------------------------------------------------------
                # connection established
                # --------------------------------------------------------------

                logger.info("Starting video reading loop")
                while cap.isOpened() and not self.error_event.is_set():
                    # continue to read frames until the connection is live and no halting event is set.
                    # if the loop exits due to connection breakdown, the outer loop retries to establish the connection.
                    # if the loop exits due to error_event, the outer loop exits as well,
                    # and the final cleanup code is executed.

                    success, frame = cap.read()
                    timestamp = time()  # capture time: recorded immediately after cap.read() returns
                    frame_id += 1
                    # cap.read() is a blocking operation !!!!
                    # still, if nothing has arrived within the timeout time, wait a bit to retry.

                    # --------- read failure ---------
                    if (not success) or (frame is None) or (frame.size == 0):
                        total_read_failures += 1
                        consecutive_read_failures += 1
                        logger.warning(
                            f"Frame {frame_id} read failed. "
                            f"N. Consecutive read failures: {consecutive_read_failures} "
                            f"(max: {self.config.frame_read_max_consecutive_failures}). "
                            f"N. Total read failures: {total_read_failures}."
                        )
                        if consecutive_read_failures < self.config.frame_read_max_consecutive_failures:
                            logger.warning(
                                f"Attempting new read "
                                f"in {self.config.frame_read_retry_delay_s} seconds..."
                            )
                            sleep(self.config.frame_read_retry_delay_s)
                            continue
                        else:
                            logger.error(
                                "Max consecutive frame read failures surpassed. "
                                f"Trying to reconnect in {self.config.connect_retry_delay_s} seconds..."
                            )
                            sleep(self.config.connect_retry_delay_s)
                            break
                            # breaks out of the inner loop, go back to the outer loop and try to reconnect

                    # --------- read successful, check aspect ratio and resize ---------

                    # check that the video frames are in the expected 16:9 aspect ratio.
                    # failure here must cause a shutdown of the application,
                    # which is only intended to process 16:9 images.
                    frame_height, frame_width, _ = frame.shape
                    aspect_ratio = frame_width / frame_height
                    if not abs(aspect_ratio - self.config.expected_aspect_ratio) < 0.02:  # tolerance: accounts for encoder padding (e.g. 1920x1088)
                        logger.error(
                            f"Application expects aspect ratio (W/H)={self.config.expected_aspect_ratio} "
                            f"but got frame of size W/H = {frame_width}/{frame_height} = {aspect_ratio}. "
                            "Shutting down the application ..."
                        )
                        self.error_event.set()
                        logger.info("Error event set")
                        break
                        # after break, skip to the end of this inner loop,
                        # enter the outer loop which terminates due to error_event being set,
                        # causing a jump to the final cleanup code.

                    # resize to desired frame size, here (1280, 720) as a compromise between resolution and speed.
                    # failure here can simply cause a warning, and resizing the next frame will be attempted.
                    try:
                        frame = cv2.resize(
                            frame, 
                            self.config.processing_shape, 
                            interpolation=cv2.INTER_LINEAR,
                        )
                    except cv2.error:
                        total_read_failures += 1
                        consecutive_read_failures += 1
                        logger.warning(
                            f"Frame {frame_id} resize failed. "
                            f"N. Consecutive read failures: {consecutive_read_failures} "
                            f"(max: {self.config.frame_read_max_consecutive_failures}). "
                            f"N. Total read failures: {total_read_failures}."
                        )
                        if consecutive_read_failures < self.config.frame_read_max_consecutive_failures:
                            logger.warning(
                                f"Attempting new read "
                                f"in {self.config.frame_read_retry_delay_s} seconds..."
                            )
                            sleep(self.config.frame_read_retry_delay_s)
                            continue
                        else:
                            logger.error(
                                "Max consecutive frame resize failures surpassed. "
                                "Shutting down the application ..."
                            )
                            self.error_event.set()
                            logger.info("Error event set")
                            break

                    # --------- read successful and checks passed: write to shared memory ---------

                    # Reset failure counter on successful read and checks passed
                    consecutive_read_failures = 0

                    # Acquire a free buffer slot. If none is available the consumer is too slow:
                    # drop this frame so the next one can be written when a slot is freed.
                    slot_idx = self.output_frame_buffer.acquire()
                    if slot_idx is None:
                        logger.warning(
                            f"No free slot in frame buffer. Consumer too slow. "
                            f"Frame {frame_id} discarded."
                        )
                        continue

                    # Write the frame zero-copy into shared memory
                    self.output_frame_buffer.write(slot_idx, frame)

                    # Build the lightweight metadata message carrying the slot reference
                    meta = FrameSlotMetadata(
                        frame_id=frame_id,
                        timestamp=timestamp,
                        original_wh=(frame_width, frame_height),
                        slot_index=slot_idx,
                    )

                    # Put the metadata on the output queue so the next process can locate the frame
                    # no need to sleep on failure since we already waited during the put timeout
                    try:
                        self.output_meta_queue.put(meta, timeout=self.config.queue_timeout)
                        logger.debug(f"Frame {frame_id} → slot {slot_idx}, metadata queued.")
                    except QueueFullException:
                        # Return the slot to the free pool so it can be reused; drop this frame
                        self.output_frame_buffer.release(slot_idx)
                        logger.warning(
                            f"Metadata queue full. Frame {frame_id} discarded. "
                            "Consumer too slow or stopped?"
                        )

                    # end of successful frame read — move on to read the next frame

            # Propagate termination signal via poison pill on clean shutdown.
            # Reaching here without error_event means connection retries were exhausted,
            # which is the "clean" end-of-stream signal for this pipeline.
            # In case of error_event, all processes stop where they are, so no pill is needed.
            # If sending the poison pill fails, set the error event to force-stop downstream processes.
            if not self.error_event.is_set():
                try:
                    logger.info("Attempting to put sentinel value on output metadata queue ...")
                    self.output_meta_queue.put(POISON_PILL, timeout=self.config.poison_pill_timeout)
                    logger.info("Sentinel value has been passed on to the next process.")
                except Exception as e:
                    logger.error(f"Error sending Poison Pill: {e}")
                    self.error_event.set()
                    logger.warning(
                        "Error event set: "
                        "force-stopping downstream processes as poison pill could not be delivered."
                    )
            else:
                # error event has been set: all processes will stop where they are.
                logger.info("Terminating and skipping Poison Pill sending. Error event is set.")

        except Exception as e:
            logger.critical(f"An unexpected error happened in StreamVideoReader: {e}")
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:

            # Final cleanup

            if cap is not None:
                try:
                    logger.info("Closing VideoCapture object ...")
                    cap.release()
                    logger.info("VideoCapture object closed")
                except Exception as e:
                    logger.error(f"Failed to close the VideoCapture object: {e}")

            self.output_frame_buffer.close()

            # log process conclusion
            logger.info(
                "StreamVideoReader process stopped gracefully. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()


if __name__ == "__main__":

    from time import time

    VIDEO_STREAM_URL = "rtsp://0.0.0.0:8554/annot"
    FRAME_SHAPE = (720, 1280, 3)   # (H, W, C) — numpy convention
    N_SLOTS = 3
    META_QUEUE_SIZE = 3

    error_event = mp.Event()
    output_meta_queue = mp.Queue(maxsize=META_QUEUE_SIZE)
    output_frame_buffer = FrameBuffer(frame_shape=FRAME_SHAPE, n_slots=N_SLOTS)

    config = StreamVideoReaderConfig(video_stream_url=VIDEO_STREAM_URL)

    reader = StreamVideoReader(
        output_meta_queue=output_meta_queue,
        output_frame_buffer=output_frame_buffer,
        error_event=error_event,
        config=config,
    )

    def consumer_loop():
        """Simple consumer: reads frames from shared memory and logs throughput."""
        frames_received = 0
        start = time()
        while True:
            try:
                msg = output_meta_queue.get(timeout=5.0)
            except Exception:
                break
            if isinstance(msg, str) and msg == POISON_PILL:
                output_meta_queue.put(POISON_PILL)  # re-queue for any other consumer
                break
            if error_event.is_set():
                break
            assert isinstance(msg, FrameSlotMetadata)
            output_frame_buffer.read(msg.slot_index)  # would be processed here
            output_frame_buffer.release(msg.slot_index)
            frames_received += 1
            elapsed = time() - start
            print(
                f"[Consumer] frame_id={msg.frame_id} "
                f"slot={msg.slot_index} "
                f"fps={frames_received / elapsed:.1f}"
            )

    import threading
    consumer_thread = threading.Thread(target=consumer_loop, daemon=True)
    consumer_thread.start()

    reader.start()
    reader.join()
    consumer_thread.join(timeout=10.0)

    output_frame_buffer.unlink()
    print("[Main] Done.")
