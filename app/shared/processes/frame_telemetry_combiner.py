import asyncio
import logging
import multiprocessing
import multiprocessing.synchronize
import ssl
import threading
from collections import deque
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException
from time import time
from typing import Literal, Optional

import multiprocessing as mp
from aiomqtt import Client, TLSParameters
from aiomqtt.exceptions import MqttError
from pydantic import BaseModel, PositiveFloat, PositiveInt, Field

from app.shared.processes.constants import (
    FRAMETELCOMB_MAX_TELEM_BUFFER_SIZE,
    FRAMETELCOMB_MAX_TIME_DIFF,
    PIPELINE_QUEUE_TIMEOUT,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
    TELEMETRY_LISTENER_HOST,
    TELEMETRY_LISTENER_MAX_INCOMING_MESSAGES,
    TELEMETRY_LISTENER_MSG_WAIT_TIMEOUT,
    TELEMETRY_LISTENER_PORT,
    TELEMETRY_LISTENER_QOS_LEVEL,
    TELEMETRY_LISTENER_RECONNECT_DELAY,
    TELEMETRY_LISTENER_TEMPLATE_TELEMETRY,
    TELEMETRY_LISTENER_TOPICS_TO_SUBSCRIBE,
    TELEMETRY_LISTENER_TOPICS_TO_TELEMETRY_MAPPING,
)
from app.shared.processes.frame_buffer import FrameBuffer
from app.shared.processes.messages import CombinedSlotMetadata, FrameSlotMetadata, TelemetryQueueObject


# ================================================================

logger = logging.getLogger("main.frame_telemetry_combiner")

if not logger.handlers:  # Avoid duplicate handlers
    _handler = logging.FileHandler('./logs/frame_telemetry_combiner.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.WARNING)

# ================================================================


class FrameTelemetryCombinerConfig(BaseModel):
    """Configuration for FrameTelemetryCombiner."""

    # MQTT connection
    mqtt_protocol: Literal["mqtt", "mqtts"] = "mqtt"
    mqtt_broker_host: str = TELEMETRY_LISTENER_HOST
    mqtt_broker_port: int = Field(default=TELEMETRY_LISTENER_PORT, ge=1, le=65535)
    mqtt_username: Optional[str] = None
    mqtt_password: Optional[str] = None
    mqtt_qos_level: Literal[0, 1, 2] = TELEMETRY_LISTENER_QOS_LEVEL
    mqtt_max_msg_wait_s: PositiveFloat = TELEMETRY_LISTENER_MSG_WAIT_TIMEOUT
    mqtt_reconnect_delay_s: PositiveFloat = TELEMETRY_LISTENER_RECONNECT_DELAY
    mqtt_ca_certs_path: Optional[str] = None          # required for MQTTS
    mqtt_cert_validation: Optional[int] = ssl.CERT_REQUIRED
    mqtt_max_incoming_messages: PositiveInt = TELEMETRY_LISTENER_MAX_INCOMING_MESSAGES

    # Telemetry–frame timestamp matching
    telemetry_buffer_max_size: PositiveInt = FRAMETELCOMB_MAX_TELEM_BUFFER_SIZE
    max_time_diff_s: PositiveFloat = FRAMETELCOMB_MAX_TIME_DIFF

    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT

    # Shutdown
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT



class FrameTelemetryCombiner(mp.Process):
    """
    Second process in the pipeline.

    Consumes video frames from StreamVideoReader via shared memory (FrameBuffer +
    lightweight metadata queue) while concurrently collecting MQTT telemetry in a
    background thread. For each frame, the best-matching telemetry entry is found by
    timestamp and the combined result is written to the output FrameBuffer for the next
    process to consume.

    The MQTT thread and the frame loop run in the same OS process:
    - MQTT thread: asyncio.run() inside a threading.Thread; updates a deque of
      TelemetryQueueObject snapshots, protected by a threading.Lock.
    - Frame loop (main thread): reads metadata from input_meta_queue, retrieves the
      frame from input_frame_buffer, matches telemetry from the deque, writes the
      frame to output_frame_buffer, and queues CombinedSlotMetadata downstream.

    MQTT is treated as non-critical: if the MQTT thread crashes, the frame loop
    continues and produces combined results with telemetry=None rather than stopping
    the pipeline.

    Termination:
    - Clean shutdown: POISON_PILL received from the input metadata queue is propagated
      to the output metadata queue so downstream processes flush and stop in order.
    - Error shutdown: if error_event is set by any process, the frame loop and the
      MQTT thread stop immediately without flushing.

    Frame drop policy: if no output buffer slot is free (consumer too slow) or the
    output metadata queue is full, the current frame is discarded.
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: FrameBuffer,
            output_meta_queue: mp.Queue,
            output_frame_buffer: FrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: FrameTelemetryCombinerConfig,
    ):
        super().__init__(name="FrameTelemetryCombiner")

        # Input: frames arrive as lightweight metadata referencing input shared memory slots
        self.input_meta_queue = input_meta_queue
        self.input_frame_buffer = input_frame_buffer

        # Output: combined result written to the next hop's shared memory
        self.output_meta_queue = output_meta_queue
        self.output_frame_buffer = output_frame_buffer

        # Shared error event — set by any process on unexpected failure
        self.error_event = error_event

        self.config = config

        self.work_finished = mp.Event()

        # Threading objects are intentionally left as None here and created in run().
        # mp.Process with 'spawn' start method pickles __init__ state to the child.
        # threading.Event and threading.Lock are not picklable, so they must be
        # created inside run() which executes in the child process.
        self._mqtt_stop: Optional[threading.Event] = None
        self._telemetry_deque: Optional[deque] = None
        self._telemetry_lock: Optional[threading.Lock] = None
        self._telemetry_state: Optional[dict] = None

    # ================================================================
    # MQTT background thread
    # ================================================================

    def _create_mqtt_client(self) -> Client:
        """Build an aiomqtt Client from the config."""
        tls_params = None
        if self.config.mqtt_protocol == "mqtts":
            # TLS is required for MQTTS; a CA certificate file must be provided
            tls_params = TLSParameters(
                ca_certs=self.config.mqtt_ca_certs_path,
                cert_reqs=self.config.mqtt_cert_validation,
            )
        return Client(
            hostname=self.config.mqtt_broker_host,
            port=self.config.mqtt_broker_port,
            tls_params=tls_params,
            username=self.config.mqtt_username,
            password=self.config.mqtt_password,
            max_queued_incoming_messages=self.config.mqtt_max_incoming_messages,
        )

    async def _mqtt_process_messages(self, client: Client) -> None:
        """
        Inner async message loop.
        Updates the rolling telemetry state on each message and appends a timestamped
        snapshot to the shared deque so the frame loop can match it by timestamp.
        """
        while not (self._mqtt_stop.is_set() or self.error_event.is_set()):

            # Wait for the next message for at most mqtt_max_msg_wait_s seconds.
            # Using a timeout allows periodic checks of the stopping conditions.
            # This does not catch MqttError — that propagates to the outer worker.
            try:
                message = await asyncio.wait_for(
                    anext(client.messages),
                    timeout=self.config.mqtt_max_msg_wait_s,
                )
                logger.debug(f"MQTT message received on topic {message.topic.value}")
            except asyncio.TimeoutError:
                # No message within timeout window — loop back to check stop conditions
                logger.debug(
                    f"No MQTT messages received for {self.config.mqtt_max_msg_wait_s}s. "
                    "Continuing to listen ..."
                )
                continue
            except StopAsyncIteration:
                # The async iterator closed (broker disconnected)
                # Let this break the loop so the outer worker handles reconnection
                logger.warning("MQTT client disconnected. Attempting to reconnect ...")
                break

            # ---------- message received ----------

            topic = message.topic.value
            try:
                # 1. Decode and update rolling telemetry state
                payload = message.payload.decode()
                telemetry_key = TELEMETRY_LISTENER_TOPICS_TO_TELEMETRY_MAPPING.get(topic)
                if not telemetry_key:
                    logger.warning(f"MQTT message from unexpected topic '{topic}'. Skipped.")
                    continue  # Ignore unmapped topics
                self._telemetry_state[telemetry_key] = float(payload)
            except Exception as e:
                logger.error(
                    f"Error processing MQTT message on topic '{topic}': {e}. "
                    "Continuing to listen ..."
                )
                continue
                # Continue to the next message: message-level error, connection still alive

            # 2. Append a timestamped snapshot to the shared deque.
            #    The lock is held only for the O(1) deque append.
            snapshot = TelemetryQueueObject(
                telemetry=self._telemetry_state.copy(),
                timestamp=time()
            )
            with self._telemetry_lock:
                self._telemetry_deque.append(snapshot)

    async def _mqtt_subscriber_worker(self) -> None:
        """
        Async MQTT worker: connects, subscribes, and loops on messages.
        A fresh client is created at every (re)connection attempt for clean state.
        Reconnects automatically on MQTT-level, network, and TLS errors.
        """
        reconnect_str = f"Retrying in {self.config.mqtt_reconnect_delay_s} seconds ..."

        # Use a non-blocking check to see if we should stop.
        while not (self._mqtt_stop.is_set() or self.error_event.is_set()):

            # Create MQTT client.
            # The client is recreated as a clean object at every disconnection.
            # Safer: discard potentially dirty old object states from reusing the same old client
            client = self._create_mqtt_client()

            try:
                logger.info(
                    f"MQTT: connecting to "
                    f"{self.config.mqtt_broker_host}:{self.config.mqtt_broker_port} ..."
                )

                # The 'async with' context manager handles connection/disconnection
                async with client:
                    logger.info("MQTT: connected. Subscribing to topics ...")

                    # Subscribe to all topics
                    for topic in TELEMETRY_LISTENER_TOPICS_TO_SUBSCRIBE:
                        await client.subscribe(topic=topic, qos=self.config.mqtt_qos_level)
                        logger.info(
                            f"MQTT: subscribed to '{topic}' (QoS {self.config.mqtt_qos_level})"
                        )

                    # Process messages using an async iterator.
                    # The message task blocks until _mqtt_stop/error_event is set,
                    # the connection drops, or an unhandled exception propagates.
                    await asyncio.create_task(self._mqtt_process_messages(client))

            except MqttError as e:
                # Handles MQTT-specific errors (network disconnect, broker kicks client)
                logger.error(f"MQTT error: {e}. {reconnect_str}")
                await asyncio.sleep(self.config.mqtt_reconnect_delay_s)

            except ConnectionRefusedError:
                # Handles initial connection failures (broker port closed/down)
                logger.error(f"MQTT connection refused. {reconnect_str}")
                await asyncio.sleep(self.config.mqtt_reconnect_delay_s)

            except ssl.SSLError as e:
                # Handles errors during the TLS handshake (e.g., invalid certificate)
                logger.error(f"MQTT TLS/SSL error: {e}. {reconnect_str}")
                await asyncio.sleep(self.config.mqtt_reconnect_delay_s)

            except Exception as e:
                # Catch all other unexpected errors
                logger.error(f"MQTT unexpected error: {e}. {reconnect_str}")
                await asyncio.sleep(self.config.mqtt_reconnect_delay_s)

    def _mqtt_thread_worker(self) -> None:
        """
        Entry point for the MQTT background thread.
        Runs an asyncio event loop dedicated to MQTT message collection.

        MQTT is non-critical: if this thread crashes, the frame loop continues
        and will produce combined results with telemetry=None.
        """
        logger.info(f"MQTT background thread started (PID {self.pid})")
        try:
            # Start the asyncio event loop and run the main MQTT worker coroutine
            asyncio.run(self._mqtt_subscriber_worker())
        except Exception as e:
            # If the thread crashes outside the worker loop
            logger.critical(
                f"MQTT background thread crashed fatally: {e}. "
                "Processing will continue without telemetry."
            )
            # DO NOT set error_event — pipeline continues without telemetry
        finally:
            logger.info("MQTT background thread stopped.")

    # ================================================================
    # Telemetry timestamp matching
    # ================================================================

    def _find_best_match(self, frame_timestamp: float) -> Optional[dict]:
        """
        Find the best-matching telemetry entry for the given frame timestamp.
        Prunes entries that are too old relative to frame_timestamp.

        This method must be called while holding self._telemetry_lock so that
        the MQTT thread cannot append to the deque concurrently with the search
        and pruning operations.

        Args:
            frame_timestamp: Timestamp of the frame to match.

        Returns:
            Best-matching telemetry dict, or None if no entry within max_time_diff_s.
        """
        if not self._telemetry_deque:
            logger.info(f"No telemetry available for matching at timestamp {frame_timestamp:.3f}")
            return None

        best_match = None
        best_diff = float('inf')
        best_idx = -1       # Index of the entry with timestamp closest to the frame
        last_too_old_idx = -1  # Last entry that is older than the matching window

        min_valid_timestamp = frame_timestamp - self.config.max_time_diff_s

        # Find closest telemetry by timestamp
        for idx, telemetry_obj in enumerate(self._telemetry_deque):
            time_diff = abs(telemetry_obj.timestamp - frame_timestamp)

            if time_diff < best_diff:
                best_diff = time_diff
                best_match = telemetry_obj
                best_idx = idx

            # Track the last telemetry entry that is outside the match window (too old)
            if telemetry_obj.timestamp < min_valid_timestamp:
                last_too_old_idx = idx

            # Since telemetry entries are ordered by arrival time, stop searching
            # once we are past the frame timestamp by more than the allowed window
            if telemetry_obj.timestamp > frame_timestamp + self.config.max_time_diff_s:
                break

        # Remove old telemetry regardless of match success
        if last_too_old_idx >= 0:
            for _ in range(last_too_old_idx + 1):
                self._telemetry_deque.popleft()
            logger.debug(
                f"Removed {last_too_old_idx + 1} telemetry entries "
                f"older than the maximum allowed time difference for matching "
                f"({self.config.max_time_diff_s} seconds)"
            )
            # Adjust best_idx since some entries were removed from the front
            if best_idx >= 0:
                best_idx = best_idx - (last_too_old_idx + 1)

        # Check if best match is within the allowed time difference
        if best_diff <= self.config.max_time_diff_s:
            logger.debug(f"Found telemetry match with time diff: {best_diff:.4f}s")
            # Remove all entries older than the matched one (keep the matched entry at front)
            removed_older = 0
            for _ in range(best_idx):
                self._telemetry_deque.popleft()
                removed_older += 1
            logger.debug(f"Removed {removed_older} telemetries older than the best match from the buffer")
            return best_match.telemetry
        else:
            logger.warning(
                f"No telemetry match found within {self.config.max_time_diff_s} seconds "
                f"(best diff: {best_diff:.4f}s)"
            )
            return None

    # ================================================================
    # Main frame-combination loop
    # ================================================================

    def run(self) -> None:
        """Main process entry point."""

        logger.info("FrameTelemetryCombiner process started")

        # Initialize threading primitives inside run() — they belong to this child process
        self._mqtt_stop = threading.Event()
        self._telemetry_deque = deque(maxlen=self.config.telemetry_buffer_max_size)
        self._telemetry_lock = threading.Lock()
        # Initial telemetry state: a copy of the template so all keys are present from the start
        self._telemetry_state = TELEMETRY_LISTENER_TEMPLATE_TELEMETRY.copy()

        # Start the MQTT collection thread.
        # Daemon: automatically killed if the process exits before join() completes.
        mqtt_thread = threading.Thread(
            target=self._mqtt_thread_worker,
            name="mqtt-collector",
            daemon=True,
        )
        mqtt_thread.start()

        failed_matches = 0
        consecutive_failed_matches = 0
        poison_pill_received = False

        try:

            # Process runs until the error_event is set or a poison pill arrives
            while not self.error_event.is_set():

                # ---- pull next frame metadata from the input queue ----
                # Short timeout to allow periodic checks of error_event
                try:
                    meta = self.input_meta_queue.get(timeout=self.config.queue_timeout)
                except QueueEmptyException:
                    logger.debug("Input metadata queue empty. Waiting for frames ...")
                    continue

                # If the object found is the poison pill, stop the frame loop.
                # The pill will be propagated downstream after the loop exits.
                if isinstance(meta, str) and meta == POISON_PILL:
                    logger.info("Poison pill received from upstream.")
                    poison_pill_received = True
                    break

                assert isinstance(meta, FrameSlotMetadata)

                # ---- zero-copy view of input slot ----
                frame = self.input_frame_buffer.view(meta.slot_index)

                # ---- find the best-matching telemetry by timestamp ----
                # The lock must be held during the entire search+prune operation
                # to prevent the MQTT thread from appending concurrently
                with self._telemetry_lock:
                    matched_telemetry = self._find_best_match(meta.timestamp)

                if matched_telemetry is None:
                    failed_matches += 1
                    consecutive_failed_matches += 1
                    logger.debug(
                        f"Frame {meta.frame_id}: no telemetry match. "
                        f"N. Consecutive failed matches: {consecutive_failed_matches}. "
                        f"N. Total failed matches: {failed_matches}. "
                        "Either the delay between frames and telemetry is too large, "
                        "or telemetry collection has stopped."
                    )
                else:
                    consecutive_failed_matches = 0

                # ---- acquire an output slot and write the frame to output shared memory ----
                out_slot = self.output_frame_buffer.acquire()
                if out_slot is None:
                    self.input_frame_buffer.release(meta.slot_index)
                    logger.warning(
                        f"No free slot in output frame buffer. "
                        f"Frame {meta.frame_id} discarded. Consumer too slow?"
                    )
                    continue

                self.output_frame_buffer.write(out_slot, frame)
                self.input_frame_buffer.release(meta.slot_index)

                # ---- put lightweight combined metadata on the output queue ----
                out_meta = CombinedSlotMetadata(
                    frame_id=meta.frame_id,
                    timestamp=meta.timestamp,
                    original_wh=meta.original_wh,
                    slot_index=out_slot,
                    telemetry=matched_telemetry,
                )

                # no need to sleep on failure since we already waited during the put timeout
                try:
                    self.output_meta_queue.put(out_meta, timeout=self.config.queue_timeout)
                    logger.debug(
                        f"Frame {meta.frame_id} → output slot {out_slot}, "
                        f"telemetry={'matched' if matched_telemetry else 'None'}."
                    )
                except QueueFullException:
                    # Return the output slot to the free pool since no metadata was queued —
                    # the consumer will never know to release it otherwise
                    self.output_frame_buffer.release(out_slot)
                    logger.warning(
                        f"Output metadata queue full. Frame {meta.frame_id} discarded. "
                        "Consumer too slow or stopped?"
                    )

                # end of frame processing — move on to the next frame

            # Propagate termination signal via poison pill on clean shutdown.
            # Reaching here without error_event means the upstream sent a pill (expected end-of-stream).
            # In case of error_event, all processes stop where they are, so no pill is needed.
            # If sending the poison pill fails, set the error event to force-stop downstream processes.
            if not self.error_event.is_set():
                try:
                    logger.info("Propagating poison pill to downstream process ...")
                    self.output_meta_queue.put(POISON_PILL, timeout=self.config.poison_pill_timeout)
                    logger.info("Poison pill propagated to downstream.")
                except Exception as e:
                    logger.error(f"Failed to propagate poison pill: {e}")
                    self.error_event.set()
                    logger.warning(
                        "Error event set: "
                        "force-stopping downstream processes as poison pill could not be delivered."
                    )
            else:
                # error event has been set: all processes will stop where they are
                logger.info("Terminating and skipping poison pill propagation. Error event is set.")

        except Exception as e:
            logger.critical(f"Unexpected error in FrameTelemetryCombiner: {e}")
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:

            # Signal and wait for the MQTT background thread to finish
            self._mqtt_stop.set()
            mqtt_thread.join(timeout=self.config.mqtt_reconnect_delay_s + 1.0)
            if mqtt_thread.is_alive():
                # Daemon threads die automatically with the process, but log the anomaly
                logger.warning(
                    "MQTT thread did not stop within the allotted time. "
                    "It will be killed when the process exits."
                )

            # Detach from shared memory in this process.
            # The parent is responsible for calling unlink() after all processes have finished.
            self.input_frame_buffer.close()
            self.output_frame_buffer.close()

            # Log process conclusion
            logger.info(
                "FrameTelemetryCombiner process stopped. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()


if __name__ == "__main__":

    import numpy as np
    from time import sleep
    from queue import Empty as QueueEmptyException

    FRAME_SHAPE = (720, 1280, 3)  # (H, W, C) — numpy convention
    N_SLOTS = 3

    # Producer & Consumer read frequency — set manually to test slow/medium/fast consumer behaviour.
    # slow=10, medium=30, fast=50  (fps)
    PRODUCER_FPS = 20
    CONSUMER_FPS = 30

    # Set to True to trigger error_event after 10 s, testing clean error-path shutdown.
    TRIGGER_ERROR_AFTER_10S = False

    _PRODUCER_FRAME_INTERVAL = 1.0 / PRODUCER_FPS
    _CONSUMER_FRAME_INTERVAL = 1.0 / CONSUMER_FPS

    error_event = mp.Event()

    input_meta_queue = mp.Queue(maxsize=N_SLOTS)
    input_frame_buffer = FrameBuffer(frame_shape=FRAME_SHAPE, n_slots=N_SLOTS)

    output_meta_queue = mp.Queue(maxsize=N_SLOTS)
    output_frame_buffer = FrameBuffer(frame_shape=FRAME_SHAPE, n_slots=N_SLOTS)

    config = FrameTelemetryCombinerConfig(
        mqtt_broker_host="127.0.0.1",
        mqtt_broker_port=TELEMETRY_LISTENER_PORT,
        mqtt_cert_validation=None,  # no TLS in test
    )

    combiner = FrameTelemetryCombiner(
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
                meta = FrameSlotMetadata(
                    frame_id=frame_id,
                    timestamp=time(),
                    original_wh=(1920, 1080),
                    slot_index=slot,
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
        # Signal end-of-stream after error or external stop
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
            assert isinstance(msg, CombinedSlotMetadata)
            output_frame_buffer.release(msg.slot_index)
            frames_received += 1
            elapsed = time() - start
            print(
                f"[Consumer] frame_id={msg.frame_id} "
                f"slot={msg.slot_index} "
                f"telemetry={'yes' if msg.telemetry else 'None'} "
                f"fps={frames_received / elapsed:.1f}"
            )
            # Throttle to the configured consumer fps
            elapsed_iter = time() - iter_start
            remaining = _CONSUMER_FRAME_INTERVAL - elapsed_iter
            if remaining > 0:
                sleep(remaining)

    def error_trigger():
        sleep(10)
        print("[ErrorTrigger] Setting error event after 10 s.")
        error_event.set()

    prod_thread = threading.Thread(target=producer_loop, daemon=True)
    cons_thread = threading.Thread(target=consumer_loop, daemon=True)

    print("[Main] Starting combiner ...")
    combiner.start()
    sleep(0.5)  # let the combiner process fully start before feeding it

    print("[Main] Starting consumer ...")
    cons_thread.start()

    print("[Main] Starting producer ...")
    prod_thread.start()

    if TRIGGER_ERROR_AFTER_10S:
        error_trigger_thread = threading.Thread(target=error_trigger, daemon=True)
        error_trigger_thread.start()

    combiner.join()
    prod_thread.join(timeout=5.0)
    cons_thread.join(timeout=5.0)

    input_frame_buffer.unlink()
    output_frame_buffer.unlink()
    print("[Main] Done.")
