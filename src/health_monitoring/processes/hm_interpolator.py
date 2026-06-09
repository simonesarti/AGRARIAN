import multiprocessing as mp
import multiprocessing.synchronize
import logging
from dataclasses import dataclass
from queue import Empty as QueueEmptyException, Full as QueueFullException
from time import time
from typing import Optional

import numpy as np
from pydantic import BaseModel, PositiveFloat

from src.health_monitoring.anomaly_detection.detector import FrameAnomalyResult
from src.health_monitoring.processes.messages import HMAnomalySlotMetadata
from src.health_monitoring.tracking.yolo_tracker import TrackState
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.constants import (
    PIPELINE_QUEUE_TIMEOUT,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
)


# ================================================================

logger = logging.getLogger("main.hm_interpolator")

if not logger.handlers:
    _handler = logging.FileHandler('./logs/hm_interpolator.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.WARNING)

# ================================================================


@dataclass
class _GroupFrame:
    """One frame held between receipt and output. SHM slot stays acquired until _emit releases it."""
    meta: HMAnomalySlotMetadata
    frame: np.ndarray   # zero-copy view into the input SHM slot


class HMVideoInterpolatorConfig(BaseModel):
    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT


class HMVideoInterpolatorProcess(mp.Process):
    """
    Temporal bbox interpolation stage, sitting between the anomaly detector
    and the annotation worker.

    Receives HMAnomalySlotMetadata from the anomaly worker where every
    (frame_skip+1)-th frame is a keyframe (full tracks + anomaly result) and
    the frames in between are passthrough frames (empty tracks/result,
    is_keyframe=False).

    Accumulates each group [K0, P1 … Pn] and waits for the next keyframe K1.
    On receipt of K1:
      - Outputs K0 with its original annotations.
      - For each Pi: linearly interpolates bbox positions between K0 and K1
        for tracks present in both keyframes. Anomaly state is copied from K0.
        Tracks absent in K1 are dropped immediately (not projected into Pi).
        Tracks that only appear in K1 are first shown at K1.
      - K1 becomes the new K0; the cycle repeats.

    On shutdown (POISON_PILL or error): flushes K0 and any buffered passthrough
    frames using K0-only annotations (last known positions, no interpolation).

    SHM note: input slots are held (zero-copy) until each frame is written to
    the output buffer in _emit, exactly like every other pipeline worker.
    The input buffer must be sized to hold a full group simultaneously plus
    backpressure headroom: frame_skip + 4 (group size frame_skip+2, plus +2
    slack like every other stage). The output buffer uses the same size to
    absorb the burst of frame_skip+1 frames emitted by _flush_group.
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: FrameBuffer,
            output_meta_queue: mp.Queue,
            output_frame_buffer: FrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: HMVideoInterpolatorConfig,
    ):
        super().__init__()

        self.input_meta_queue = input_meta_queue
        self.input_frame_buffer = input_frame_buffer
        self.output_meta_queue = output_meta_queue
        self.output_frame_buffer = output_frame_buffer
        self.error_event = error_event
        self.config = config

        self.work_finished = mp.Event()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):

        logger.info("HMVideoInterpolatorProcess started.")
        poison_pill_received = False

        _k0: Optional[_GroupFrame] = None
        _pending: list[_GroupFrame] = []

        try:

            while not self.error_event.is_set():
                
                # ----------------------- FETCH ------------------------------------
                try:
                    meta = self.input_meta_queue.get(timeout=self.config.queue_timeout)
                except QueueEmptyException:
                    logger.debug("Input queue empty. Waiting ...")
                    continue

                if isinstance(meta, str) and meta == POISON_PILL:
                    logger.info("Poison pill received.")
                    poison_pill_received = True
                    break

                assert isinstance(meta, HMAnomalySlotMetadata)

                # ----------------------- COLLECT AND PROCESS ------------------------------------

                # Zero-copy view; slot is held until _emit releases it after writing
                # to the output buffer — same pattern as all other pipeline workers.
                frame = self.input_frame_buffer.view(meta.slot_index)
                gf = _GroupFrame(meta=meta, frame=frame)

                if meta.is_keyframe:
                    if _k0 is not None:
                        # K1 has arrived — interpolate and flush the accumulated group.
                        self._flush_group(_k0, _pending, gf)
                    _k0 = gf
                    _pending = []
                else:
                    if _k0 is None:
                        logger.warning(f"Passthrough frame {meta.frame_id} received before any keyframe. Dropping.")
                        continue
                    _pending.append(gf)

            if not self.error_event.is_set():
                try:
                    logger.info("Attempting to put sentinel value on output queue ...")
                    self.output_meta_queue.put(POISON_PILL, timeout=self.config.poison_pill_timeout)
                    logger.info("Poison pill propagated downstream.")
                except Exception as e:
                    logger.error(f"Failed to propagate poison pill: {e}")
                    self.error_event.set()
                    logger.warning(
                        "Error event set: force-stop application since downstream process "
                        "is unable to receive the poison pill."
                    )
            else:
                logger.info("Skipping poison pill propagation. Error event is set.")

        except Exception as e:
            logger.critical(f"An unexpected critical error occurred in HMVideoInterpolatorProcess: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            self.input_frame_buffer.close()
            self.output_frame_buffer.close()
            logger.info(
                "HMVideoInterpolatorProcess stopped. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()

    # ------------------------------------------------------------------
    # Flush helpers
    # ------------------------------------------------------------------

    def _flush_group(
            self,
            k0: _GroupFrame,
            pending: list[_GroupFrame],
            k1: _GroupFrame,
    ) -> None:
        """Emit K0 with full annotations, then interpolated Pi frames and emit them."""
        self._emit(k0.frame, k0.meta.slot_index, k0.meta)

        if not pending:
            return

        n = len(pending)
        k0_by_id = {t.track_id: t for t in k0.meta.tracks}
        k1_by_id = {t.track_id: t for t in k1.meta.tracks}
        common_ids = set(k0_by_id) & set(k1_by_id)

        # Score fields are identical for every pending frame (K0 values filtered to common tracks)
        # compute once and reuse.
        r = k0.meta.anomaly_result
        filtered_ae_scores      = {tid: v for tid, v in r.ae_scores.items()     if tid in common_ids}
        filtered_social_scores  = {tid: v for tid, v in r.social_scores.items() if tid in common_ids}
        filtered_ongoing_events = [e   for e   in r.ongoing_events              if e.track_id in common_ids]
        filtered_unscored       = [tid for tid in r.unscored_tracks              if tid in common_ids]
        filtered_scored_ok      = [tid for tid in r.scored_ok_tracks             if tid in common_ids]
        filtered_ok_ae          = [tid for tid in r.ok_ae_tracks                 if tid in common_ids]
        filtered_ok_soc         = [tid for tid in r.ok_soc_tracks                if tid in common_ids]
        filtered_elevated_ae    = [tid for tid in r.elevated_ae_tracks           if tid in common_ids]
        filtered_elevated_soc   = [tid for tid in r.elevated_soc_tracks          if tid in common_ids]
        filtered_elevated_both  = [tid for tid in r.elevated_both_tracks         if tid in common_ids]
        filtered_confirmed_ae   = [tid for tid in r.confirmed_ae_tracks          if tid in common_ids]
        filtered_confirmed_soc  = [tid for tid in r.confirmed_soc_tracks         if tid in common_ids]
        filtered_confirmed_both = [tid for tid in r.confirmed_both_tracks        if tid in common_ids]

        for i, pf in enumerate(pending, start=1):
            alpha = i / (n + 1)
            interp_tracks = [
                TrackState(
                    track_id=tid,
                    bbox=(1.0 - alpha) * k0_by_id[tid].bbox + alpha * k1_by_id[tid].bbox,
                    confidence=k0_by_id[tid].confidence,
                    class_id=k0_by_id[tid].class_id,
                )
                for tid in common_ids
            ]
            interp_result = FrameAnomalyResult(
                frame_idx=pf.meta.frame_id,
                timestamp_ms=pf.meta.timestamp * 1000.0,
                ae_scores=filtered_ae_scores,
                social_scores=filtered_social_scores,
                ongoing_events=filtered_ongoing_events,
                unscored_tracks=filtered_unscored,
                scored_ok_tracks=filtered_scored_ok,
                ok_ae_tracks=filtered_ok_ae,
                ok_soc_tracks=filtered_ok_soc,
                elevated_ae_tracks=filtered_elevated_ae,
                elevated_soc_tracks=filtered_elevated_soc,
                elevated_both_tracks=filtered_elevated_both,
                confirmed_ae_tracks=filtered_confirmed_ae,
                confirmed_soc_tracks=filtered_confirmed_soc,
                confirmed_both_tracks=filtered_confirmed_both,
            )
            self._emit(
                frame=pf.frame,
                input_slot=pf.meta.slot_index,
                meta=HMAnomalySlotMetadata(
                        frame_id=pf.meta.frame_id,
                        timestamp=pf.meta.timestamp,
                        original_wh=pf.meta.original_wh,
                        slot_index=-1,           # filled in by _emit
                        tracks=interp_tracks,
                        anomaly_result=interp_result,
                        is_keyframe=True,
                ),
            )

    # ------------------------------------------------------------------
    # Output helper
    # ------------------------------------------------------------------

    def _emit(self, frame: np.ndarray, input_slot: int, meta: HMAnomalySlotMetadata) -> None:
        """Write frame to output SHM slot, release the input slot, and enqueue metadata."""
        t0 = time()
        out_slot = self.output_frame_buffer.acquire()
        if out_slot is None:
            self.input_frame_buffer.release(input_slot)
            logger.warning(f"No free output slot; frame {meta.frame_id} dropped.")
            return

        self.output_frame_buffer.write(out_slot, frame)
        self.input_frame_buffer.release(input_slot)

        out_meta = HMAnomalySlotMetadata(
            frame_id=meta.frame_id,
            timestamp=meta.timestamp,
            original_wh=meta.original_wh,
            slot_index=out_slot,
            tracks=meta.tracks,
            anomaly_result=meta.anomaly_result,
            is_keyframe=True,
        )
        try:
            self.output_meta_queue.put(out_meta, timeout=self.config.queue_timeout)
            logger.debug(
                f"[TIMING] frame={meta.frame_id} slot={out_slot} "
                f"emit={( time() - t0) * 1000:.1f}ms"
            )
        except QueueFullException:
            self.output_frame_buffer.release(out_slot)
            logger.warning(f"Output queue full; frame {meta.frame_id} dropped.")
