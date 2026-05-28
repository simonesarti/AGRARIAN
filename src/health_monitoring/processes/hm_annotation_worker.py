import multiprocessing as mp
import multiprocessing.synchronize
import logging
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException
from time import time

import cv2
import numpy as np
from pydantic import BaseModel, PositiveFloat

from src.health_monitoring.processes.messages import HMAnomalySlotMetadata
from src.health_monitoring.anomaly_detection.detector import FrameAnomalyResult
from src.shared.processes.messages import AnnotationSlotMetadata
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.constants import (
    PIPELINE_QUEUE_TIMEOUT,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
)


# ================================================================

logger = logging.getLogger("main.hm_annotation")

if not logger.handlers:
    _handler = logging.FileHandler('./logs/hm_annotation.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

# ================================================================

# Summary colour scheme (BGR)
_STEEL_BLUE = (180, 130, 70)    # unscored — insufficient history
_GREEN      = (0, 200, 0)       # scored ok
_YELLOW     = (0, 220, 220)     # elevated — score above threshold, not yet duration-confirmed
_RED        = (0, 0, 255)       # confirmed anomaly

_FONT = cv2.FONT_HERSHEY_SIMPLEX


def _track_color(tid: int, r: FrameAnomalyResult) -> tuple:
    confirmed_set = set(r.confirmed_ae_tracks) | set(r.confirmed_soc_tracks) | set(r.confirmed_both_tracks)
    elevated_set  = set(r.elevated_ae_tracks)  | set(r.elevated_soc_tracks)  | set(r.elevated_both_tracks)
    scored_set    = set(r.ae_scores)
    if tid in confirmed_set:
        return _RED
    if tid in elevated_set:
        return _YELLOW
    if tid in scored_set:
        return _GREEN
    return _STEEL_BLUE


def _annotate(frame: np.ndarray, tracks: list, r: FrameAnomalyResult) -> np.ndarray:
    """Draw per-track bounding boxes and a HUD onto frame in-place. Returns frame."""

    for t in tracks:
        x1, y1, x2, y2 = t.bbox.astype(int)
        color = _track_color(t.track_id, r)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

        ae  = r.ae_scores.get(t.track_id)
        soc = r.social_scores.get(t.track_id)
        label = f"ID:{t.track_id}"
        if ae is not None:
            label += f" ae={ae:.2f}"
        if soc is not None:
            label += f" soc={soc:.2f}"
        cv2.putText(frame, label, (x1, max(y1 - 6, 12)), _FONT, 0.42, color, 1, cv2.LINE_AA)
    
    confirmed_set = set(r.confirmed_ae_tracks) | set(r.confirmed_soc_tracks) | set(r.confirmed_both_tracks)
    elevated_set  = set(r.elevated_ae_tracks)  | set(r.elevated_soc_tracks)  | set(r.elevated_both_tracks)
    active_ids    = {t.track_id for t in tracks}

    n_elevated  = len(elevated_set  & active_ids)
    n_confirmed = len(confirmed_set & active_ids)
    hud = (
        f"Frame {r.frame_idx} | "
        f"tracks={len(tracks)} | "
        f"elevated={n_elevated} | "
        f"confirmed={n_confirmed}"
    )
    (text_w, text_h), baseline = cv2.getTextSize(hud, _FONT, 0.55, 1)
    pad = 4
    cv2.rectangle(frame,
                  (8 - pad, 22 - text_h - pad),
                  (8 + text_w + pad, 22 + baseline + pad),
                  (0, 0, 0), cv2.FILLED)
    cv2.putText(frame, hud, (8, 22), _FONT, 0.55, (255, 255, 255), 1, cv2.LINE_AA)

    return frame


class HMAnnotationWorkerConfig(BaseModel):
    """Configuration for HMAnnotationWorker."""

    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT


class HMAnnotationWorker(mp.Process):
    """
    Annotation and fan-out stage of the health monitoring pipeline.

    Reads a (H, W, 3) BGR frame at processing resolution from the input FrameBuffer
    together with HMAnomalySlotMetadata (active tracks + FrameAnomalyResult).

    For each frame:
      - Draws per-animal bounding boxes coloured by anomaly state (summary scheme):
          steel blue  — unscored (insufficient track history)
          green       — scored, neither scorer flagged
          yellow      — elevated (score above threshold, not yet duration-confirmed)
          red         — confirmed anomaly (threshold exceeded for ≥ min_anomaly_duration frames)
      - Draws a HUD with frame index, active track count, elevated and confirmed counts.
      - Upscales the annotated frame to the original video resolution.
      - Independently writes the frame to two output FrameBuffers (alert and video),
        and puts AnnotationSlotMetadata on the corresponding queues.

    alert_msg is empty when no confirmed anomaly is present, or lists the confirmed
    track IDs (e.g., "Anomaly: tracks [3, 7]") when animals are confirmed anomalous.
    The alert writer uses this field to decide whether to notify the user.

    Fan-out: slot acquisition and queue enqueue for alert and video are independent.
    A failure on one output (full queue or no free slot) does not affect the other.

    Termination:
    - Clean shutdown: POISON_PILL is propagated to both output queues.
    - Error shutdown: loop stops immediately when error_event is set.
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: FrameBuffer,
            alert_output_meta_queue: mp.Queue,
            alert_output_frame_buffer: FrameBuffer,
            video_output_meta_queue: mp.Queue,
            video_output_frame_buffer: FrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: HMAnnotationWorkerConfig,
    ):
        super().__init__()

        self.input_meta_queue = input_meta_queue
        self.input_frame_buffer = input_frame_buffer

        self.alert_output_meta_queue = alert_output_meta_queue
        self.alert_output_frame_buffer = alert_output_frame_buffer

        self.video_output_meta_queue = video_output_meta_queue
        self.video_output_frame_buffer = video_output_frame_buffer

        self.error_event = error_event
        self.config = config

        self.work_finished = mp.Event()

    def run(self):
        logger.info("HM annotation process started.")
        poison_pill_received = False

        try:

            while not self.error_event.is_set():

                iter_start = time()

                try:
                    meta = self.input_meta_queue.get(timeout=self.config.queue_timeout)
                except QueueEmptyException:
                    logger.debug("Input queue timed out. Upstream producer may be stalled. Retrying ...")
                    continue

                if isinstance(meta, str) and meta == POISON_PILL:
                    logger.info("Found sentinel value on queue.")
                    poison_pill_received = True
                    break

                assert isinstance(meta, HMAnomalySlotMetadata)

                get_time = time() - iter_start

                # ---- zero-copy view of input slot ----
                predict_start = time()

                frame = self.input_frame_buffer.view(meta.slot_index)

                r: FrameAnomalyResult = meta.anomaly_result

                # ---- annotate (in-place on the shared memory view) ----
                _annotate(frame, meta.tracks, r)

                # ---- upscale to original video resolution ----
                annotated_frame = cv2.resize(
                    src=frame,
                    dsize=meta.original_wh,     # (W, H)
                    interpolation=cv2.INTER_LINEAR,
                )
                self.input_frame_buffer.release(meta.slot_index)

                # Build alert message: only confirmed tracks visible in this frame.
                active_ids = {t.track_id for t in meta.tracks}
                confirmed = sorted(tid for tid in r.anomalous_tracks if tid in active_ids)
                alert_msg = f"Anomaly: tracks {confirmed}" if confirmed else ""

                predict_time = time() - predict_start

                # ---- fan-out: write annotated frame to both consumers independently ----
                append_start = time()

                # -- Alert output --
                alert_slot = self.alert_output_frame_buffer.acquire()
                if alert_slot is None:
                    logger.warning(
                        f"No free slot in alert output frame buffer. "
                        f"Frame {meta.frame_id} dropped for alert writer. Consumer too slow?"
                    )
                else:
                    self.alert_output_frame_buffer.write(alert_slot, annotated_frame)
                    alert_meta = AnnotationSlotMetadata(
                        frame_id=meta.frame_id,
                        timestamp=meta.timestamp,
                        slot_index=alert_slot,
                        alert_msg=alert_msg,
                    )
                    try:
                        self.alert_output_meta_queue.put(alert_meta, timeout=self.config.queue_timeout)
                        logger.debug(f"Frame {meta.frame_id} → alert slot {alert_slot}.")
                    except QueueFullException:
                        self.alert_output_frame_buffer.release(alert_slot)
                        logger.warning(
                            f"Alert output metadata queue full. Frame {meta.frame_id} dropped for alert writer. "
                            "Consumer too slow or stopped?"
                        )

                # -- Video output --
                video_slot = self.video_output_frame_buffer.acquire()
                if video_slot is None:
                    logger.warning(
                        f"No free slot in video output frame buffer. "
                        f"Frame {meta.frame_id} dropped for video writer. Consumer too slow?"
                    )
                else:
                    self.video_output_frame_buffer.write(video_slot, annotated_frame)
                    video_meta = AnnotationSlotMetadata(
                        frame_id=meta.frame_id,
                        timestamp=meta.timestamp,
                        slot_index=video_slot,
                        alert_msg=alert_msg,
                    )
                    try:
                        self.video_output_meta_queue.put(video_meta, timeout=self.config.queue_timeout)
                        logger.debug(f"Frame {meta.frame_id} → video slot {video_slot}.")
                    except QueueFullException:
                        self.video_output_frame_buffer.release(video_slot)
                        logger.warning(
                            f"Video output metadata queue full. Frame {meta.frame_id} dropped for video writer. "
                            "Consumer too slow or stopped?"
                        )

                iter_time = time() - iter_start
                logger.debug(
                    f"frame {meta.frame_id} processed in {iter_time * 1000:.2f} ms, "
                    f"of which --> "
                    f"GET: {get_time * 1000:.2f} ms, "
                    f"ANNOTATE: {predict_time * 1000:.2f} ms, "
                    f"PROPAGATE: {(time() - append_start) * 1000:.2f} ms."
                )

            # Propagate termination to both consumers.
            if not self.error_event.is_set():
                for name, q in [
                    ("alert", self.alert_output_meta_queue),
                    ("video", self.video_output_meta_queue),
                ]:
                    try:
                        logger.info(f"Attempting to put sentinel value on {name} output queue ...")
                        q.put(POISON_PILL, timeout=self.config.poison_pill_timeout)
                        logger.info(f"Sentinel value passed to {name} output queue.")
                    except Exception as e:
                        logger.error(f"Error propagating Poison Pill to {name} output queue: {e}")
                        self.error_event.set()
                        logger.warning(
                            "Error event set: force-stop application since downstream process "
                            "is unable to receive the poison pill."
                        )
            else:
                logger.info("Terminating and skipping Poison Pill sending. Error event is set.")

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in HM annotation process: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            self.input_frame_buffer.close()
            self.alert_output_frame_buffer.close()
            self.video_output_frame_buffer.close()

            logger.info(
                "HM annotation process terminated. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()
