import multiprocessing as mp
import multiprocessing.synchronize
import logging
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException
from time import time
from typing import Optional

from pydantic import BaseModel, Field, PositiveFloat

from src.health_monitoring.inference.config import AnomalyConfig, FeaturesConfig, ModelConfig
from src.health_monitoring.tracking.feature_extractor import FeatureExtractor
from src.health_monitoring.anomaly_detection.detector import AnomalyDetector, FrameAnomalyResult
from src.health_monitoring.processes.messages import HMTrackingSlotMetadata, HMAnomalySlotMetadata
from src.shared.processes.frame_buffer import FrameBuffer
from src.shared.processes.constants import (
    PIPELINE_QUEUE_TIMEOUT,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
    VIDEO_STREAM_READER_PROCESSING_SHAPE,
)


# ================================================================

logger = logging.getLogger("main.hm_anomaly")

if not logger.handlers:
    _handler = logging.FileHandler('./logs/hm_anomaly.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.WARNING)

# ================================================================


class HMAnomalyDetectionWorkerConfig(BaseModel):
    """Configuration for HMAnomalyDetectionWorker."""

    features_cfg: FeaturesConfig = Field(default_factory=FeaturesConfig)
    anomaly_cfg: AnomalyConfig = Field(default_factory=AnomalyConfig)
    model_cfg: ModelConfig = Field(default_factory=ModelConfig)
    weights_path: Optional[str] = None  # path to AE checkpoint; None = uninitialised (scores will be uncalibrated)
    device: str = "cpu"
    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT


class HMAnomalyDetectionWorker(mp.Process):
    """
    Anomaly detection stage of the health monitoring pipeline.

    Reads a (H, W, 3) BGR frame and HMTrackingSlotMetadata (carrying active
    TrackState objects and the BotSORT GMC homography H) from the input FrameBuffer.

    For each frame:
      1. FeatureExtractor.update(): computes 9-d feature vectors per track,
         with ego-motion compensation via H and topological K-NN context.
      2. AnomalyDetector.score_tracks(): updates the social scorer EMA, runs
         the AE reconstruction scorer, smooths scores over time, and classifies
         each track as unscored / ok / elevated / confirmed.

    Writes the frame unchanged to the output FrameBuffer and puts a
    HMAnomalySlotMetadata on the output queue, carrying the slot index, the
    active tracks (for bounding-box drawing), and the FrameAnomalyResult.

    Termination:
    - Clean shutdown: POISON_PILL is propagated downstream.
    - Error shutdown: loop stops immediately when error_event is set.

    Frame drop policy: if no output buffer slot is free or the output queue is
    full, the current frame is discarded.
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: FrameBuffer,
            output_meta_queue: mp.Queue,
            output_frame_buffer: FrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: HMAnomalyDetectionWorkerConfig,
    ):
        super().__init__()

        self.input_meta_queue = input_meta_queue
        self.input_frame_buffer = input_frame_buffer
        self.output_meta_queue = output_meta_queue
        self.output_frame_buffer = output_frame_buffer
        self.error_event = error_event
        self.config = config

        self.work_finished = mp.Event()

    def run(self):

        logger.info("HM anomaly detection process started.")
        poison_pill_received = False

        try:

            frame_w, frame_h = VIDEO_STREAM_READER_PROCESSING_SHAPE  # (W, H)
            extractor = FeatureExtractor(self.config.features_cfg, frame_w=frame_w, frame_h=frame_h)

            detector = AnomalyDetector(
                anomaly_cfg=self.config.anomaly_cfg,
                model_cfg=self.config.model_cfg,
                features_cfg=self.config.features_cfg,
                device=self.config.device,
            )
            if self.config.weights_path:
                detector.load_weights(self.config.weights_path)
                logger.info(f"AE weights loaded from {self.config.weights_path}.")
            else:
                logger.warning(
                    "No AE weights path provided. Reconstruction scores will be uncalibrated. "
                    "Run training first and set weights_path in anomaly_detector.yaml."
                )
            detector.set_eval()
            logger.info("Anomaly detector initialised.")

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

                assert isinstance(meta, HMTrackingSlotMetadata)

                # ---- zero-copy view of input slot ----
                frame = self.input_frame_buffer.view(meta.slot_index)

                # ---- run scoring on keyframes; assign empty results for passthrough ----
                predict_start = time()
                if meta.is_keyframe:
                    tf_map = extractor.update(
                        tracks=meta.tracks,
                        H_prev_to_curr=meta.H,
                        frame_idx=meta.frame_id,
                    )
                    # Anomaly scoring (timestamp in ms for event logging)
                    anomaly_result = detector.score_tracks(
                        track_features=tf_map,
                        frame_idx=meta.frame_id,
                        timestamp_ms=meta.timestamp * 1000.0,
                    )
                    tracks = meta.tracks
                    n_confirmed = len(anomaly_result.anomalous_tracks)
                    n_elevated = (
                        len(anomaly_result.elevated_ae_tracks) +
                        len(anomaly_result.elevated_soc_tracks) +
                        len(anomaly_result.elevated_both_tracks)
                    )
                    if n_confirmed > 0:
                        logger.info(
                            f"Frame {meta.frame_id}: {n_confirmed} confirmed anomalous tracks "
                            f"{anomaly_result.anomalous_tracks}."
                        )
                    elif n_elevated > 0:
                        logger.debug(f"Frame {meta.frame_id}: {n_elevated} elevated tracks.")
                else:
                    tracks = []
                    anomaly_result = FrameAnomalyResult(
                        frame_idx=meta.frame_id,
                        timestamp_ms=meta.timestamp * 1000.0,
                    )
                
                predict_time = time() - predict_start

                # ---- write frame to output buffer ----
                append_start = time()

                out_slot = self.output_frame_buffer.acquire()
                if out_slot is None:
                    self.input_frame_buffer.release(meta.slot_index)
                    logger.warning(
                        f"No free slot in output frame buffer. "
                        f"Frame {meta.frame_id} dropped. Consumer too slow?"
                    )
                    continue

                self.output_frame_buffer.write(out_slot, frame)
                self.input_frame_buffer.release(meta.slot_index)
                out_meta = HMAnomalySlotMetadata(
                    frame_id=meta.frame_id,
                    timestamp=meta.timestamp,
                    original_wh=meta.original_wh,
                    slot_index=out_slot,
                    tracks=tracks,
                    anomaly_result=anomaly_result,
                    is_keyframe=meta.is_keyframe,
                )
                try:
                    self.output_meta_queue.put(out_meta, timeout=self.config.queue_timeout)
                    logger.debug(f"Frame {meta.frame_id} → slot {out_slot} (keyframe={meta.is_keyframe}).")
                except QueueFullException:
                    self.output_frame_buffer.release(out_slot)
                    logger.warning(
                        f"Output metadata queue full. Frame {meta.frame_id} dropped. "
                        "Consumer too slow or stopped?"
                    )

                iter_time = time() - iter_start
                logger.debug(
                    f"frame {meta.frame_id} processed in {iter_time * 1000:.2f} ms, "
                    f"of which --> "
                    f"SCORE: {predict_time * 1000:.2f} ms, "
                    f"PROPAGATE: {(time() - append_start) * 1000:.2f} ms."
                )

            if not self.error_event.is_set():
                try:
                    logger.info("Attempting to put sentinel value on output queue ...")
                    self.output_meta_queue.put(POISON_PILL, timeout=self.config.poison_pill_timeout)
                    logger.info("Sentinel value passed to output queue.")
                except Exception as e:
                    logger.error(f"Error propagating Poison Pill: {e}")
                    self.error_event.set()
                    logger.warning(
                        "Error event set: force-stop application since downstream process "
                        "is unable to receive the poison pill."
                    )
            else:
                logger.info("Terminating and skipping Poison Pill sending. Error event is set.")

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in HM anomaly detection process: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:
            self.input_frame_buffer.close()
            self.output_frame_buffer.close()

            logger.info(
                "HM anomaly detection process terminated. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()
