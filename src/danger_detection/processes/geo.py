import multiprocessing as mp
import multiprocessing.synchronize
import logging
from queue import Empty as QueueEmptyException
from queue import Full as QueueFullException
from time import time, sleep

import numpy as np
from pydantic import BaseModel, PositiveFloat
from shapely import Polygon

from src.shared.drone_utils.gsd import get_meters_per_pixel
from src.shared.drone_utils.localization import get_objects_coordinates
from src.danger_detection.utils import (
    close_tifs,
    compute_slope_mask_horn,
    create_geofencing_mask_runtime,
    extract_dem_window,
    open_dem_tifs,
    get_frame_transform,
    get_window_size_m,
    map_window_onto_drone_frame,
)
from src.danger_detection.processes.messages import SegmentationSlotMetadata, GeoSlotMetadata
from src.shared.processes.frame_buffer import FrameBuffer, MultiFrameBuffer
from src.shared.processes.constants import (
    GEO_DEM_CACHE_BUFFER_SCALE,
    PIPELINE_QUEUE_TIMEOUT,
    POISON_PILL,
    POISON_PILL_TIMEOUT,
)


# ================================================================

logger = logging.getLogger("main.danger_geo")

if not logger.handlers:  # Avoid duplicate handlers
    _handler = logging.FileHandler('./logs/danger_geo.log', mode='w')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.WARNING)

# ================================================================

def _footprint_within_bounds(corners_lonlat: np.ndarray, bounds) -> bool:
    """Return True if all four frame corners lie inside the window bounds."""
    if bounds is None:
        return False
    min_lon, min_lat, max_lon, max_lat = bounds
    return (
        corners_lonlat[:, 0].min() >= min_lon and
        corners_lonlat[:, 0].max() <= max_lon and
        corners_lonlat[:, 1].min() >= min_lat and
        corners_lonlat[:, 1].max() <= max_lat
    )




class GeoWorkerConfig(BaseModel):
    """Configuration for GeoWorker."""

    # Domain-specific geo and drone parameters forwarded from the application config.
    # Expected keys for input_args:
    #   dem (str | None), 
    #   dem_mask (str | None), 
    #   safety_radius_m (float),
    #   slope_angle_threshold (float), 
    #   geofencing_vertexes (list | None)
    # Expected keys for drone_args:
    #   true_focal_len_mm, 
    #   sensor_width_mm,
    #   sensor_height_mm,
    #   sensor_width_pixels,
    #   sensor_height_pixels
    input_args: dict
    drone_args: dict

    # Fallback animal body length used to estimate meters-per-pixel when telemetry is absent.
    # The longer axis of each detection bbox is assumed to correspond to this length.
    # Default: 1.3 m (median adult domestic sheep).
    animal_reference_size_m: PositiveFloat = 1.3

    queue_timeout: PositiveFloat = PIPELINE_QUEUE_TIMEOUT
    poison_pill_timeout: PositiveFloat = POISON_PILL_TIMEOUT
    dem_cache_buffer_scale: PositiveFloat = GEO_DEM_CACHE_BUFFER_SCALE


class GeoWorker(mp.Process):
    """
    Geo-analysis process in the danger detection pipeline.

    Reads a MultiFrameBuffer slot from SegmentationWorker:
        primary   (H, W, 3) : BGR frame
        secondary (2, H, W) : [0] roads_mask, [1] vehicles_mask

    Runs DEM-based slope, no-data, and geofencing analysis, then writes a
    MultiFrameBuffer slot for DangerWorker:
        primary   (H, W, 3) : BGR frame (forwarded unchanged)
        secondary (5, H, W) : [0] roads_mask, [1] vehicles_mask,
                               [2] nodata_dem_mask, [3] geofencing_mask, [4] slope_mask

    When telemetry is None, geo masks default to all-zeros and safety_radius_pixels to -1.

    Termination:
    - Clean shutdown: POISON_PILL received from the input metadata queue is
      propagated to the output metadata queue.
    - Error shutdown: if error_event is set by any process, the loop stops
      immediately without flushing.

    Frame drop policy: if no output buffer slot is free (consumer too slow) or
    the output metadata queue is full, the current frame is discarded.
    """

    def __init__(
            self,
            input_meta_queue: mp.Queue,
            input_frame_buffer: MultiFrameBuffer,
            output_meta_queue: mp.Queue,
            output_frame_buffer: MultiFrameBuffer,
            error_event: multiprocessing.synchronize.Event,
            config: GeoWorkerConfig,
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
        """
        Main loop of the process: opens DEM files once, then processes frames.
        """

        logger.info("Geo-handling process started.")
        poison_pill_received = False

        # Open DEM and DEM-mask rasters once for the lifetime of this process.
        # dem_tif / dem_mask_tif may be None if paths were not provided.
        dem_tif, dem_mask_tif = open_dem_tifs(
            dem_path=self.config.input_args["dem"],
            dem_mask_path=self.config.input_args["dem_mask"],
        )

        # Build geofencing polygon once — it is constant for the lifetime of the process.
        geofencing_polygon = (
            Polygon(self.config.input_args["geofencing_vertexes"])
            if self.config.input_args["geofencing_vertexes"] is not None
            else None
        )

        # Placeholders, populated from the first frame received.
        frame_width = None
        frame_height = None
        frame_corners = None

        # DEM window cache — populated on first extraction, reused while the
        # drone field of view stays within the cached geographic bounds.
        dem_cache_bounds = None            # (min_lon, min_lat, max_lon, max_lat)
        dem_cache_masks_window = None      # (2, W, W): [nodata_mask, slope_mask]
        dem_cache_window_transform = None  # Affine transform for the cached window

        # Exact-match cache — if telemetry repeats (drone stationary),
        # skip every geo computation and reuse masks directly.
        prev_tel_key = None        # (lat, lon, alt, yaw) tuple
        prev_nodata_mask = None    # (H, W) uint8
        prev_slope_mask = None     # (H, W) uint8
        prev_geofencing_mask = None  # (H, W) uint8

        try:

            while not self.error_event.is_set():

                iter_start = time()

                # ---- pull next frame metadata from the input queue ----
                try:
                    meta = self.input_meta_queue.get(timeout=self.config.queue_timeout)
                except QueueEmptyException:
                    logger.debug("Input queue timed out. Upstream producer may be stalled. Retrying ...")
                    continue

                # ---- poison pill: propagate downstream and exit ----
                if isinstance(meta, str) and meta == POISON_PILL:
                    logger.info("Found sentinel value on queue.")
                    poison_pill_received = True
                    break

                assert isinstance(meta, SegmentationSlotMetadata)

                get_start = time()

                # ---- zero-copy views of input slot ----
                frame_view, seg_mask_view = self.input_frame_buffer.view(meta.slot_index)

                # ---- one-time frame-dimension setup from the first slot read ----
                if frame_width is None:
                    frame_height, frame_width = frame_view.shape[:2]
                    # frame corners are stored in (x,y) format
                    frame_corners = np.array([
                        [0, 0],                                 # upper left
                        [frame_width - 1, 0],                   # upper right
                        [frame_width - 1, frame_height - 1],    # bottom right
                        [0, frame_height - 1],                  # bottom left
                    ])
                    logger.info(f"Geo worker setup with frame size W×H = {frame_width}×{frame_height}")

                # ---- run geo analysis ----
                predict_start = time()

                if meta.telemetry is None:
                    # No telemetry: DEM/geofencing/slope analysis cannot be performed.
                    # Zero masks are used for those layers.
                    # Safety radius is estimated from detection bboxes if any are present:
                    # the longer axis of each bbox is assumed to equal animal_reference_size_m,
                    # giving a per-frame meters-per-pixel estimate via the median across all detections.
                    # If no detections are present, safety_radius_pixels stays at -1 (no danger reported).
                    nodata_dem_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
                    geofencing_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
                    slope_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)

                    if len(meta.classes) > 0:
                        # max(width, height) of each bbox — longer axis approximates body length
                        bbox_widths = np.abs(meta.boxes_corner2[:, 0] - meta.boxes_corner1[:, 0])
                        bbox_heights = np.abs(meta.boxes_corner2[:, 1] - meta.boxes_corner1[:, 1])
                        median_max_dim_px = float(np.median(np.maximum(bbox_widths, bbox_heights)))

                        if median_max_dim_px > 0:
                            meters_per_pixel_est = self.config.animal_reference_size_m / median_max_dim_px
                            safety_radius_pixels = int(
                                self.config.input_args["safety_radius_m"] / meters_per_pixel_est
                            )
                            logger.info(
                                f"Frame {meta.frame_id}: no telemetry — safety radius estimated from "
                                f"{len(meta.classes)} bbox(es). "
                                f"Median max bbox dim: {median_max_dim_px:.1f} px, "
                                f"estimated meters/px: {meters_per_pixel_est:.4f}, "
                                f"safety radius: {safety_radius_pixels} px."
                            )
                        else:
                            safety_radius_pixels = -1
                            logger.warning(
                                f"Frame {meta.frame_id}: no telemetry and bbox dimensions are zero. "
                                "Safety radius set to -1 — no danger will be reported."
                            )
                    else:
                        safety_radius_pixels = -1
                        logger.warning(
                            f"Frame {meta.frame_id}: no telemetry and no detections. "
                            "Safety radius set to -1 — no danger will be reported."
                        )

                else:
                    # load frame flight data
                    telemetry = meta.telemetry
                    logger.debug(f"Received telemetry: {telemetry}")

                    # ============== COMPUTE SAFETY AREA RADIUS SIZE IN PIXELS  ===================================
                    meters_per_pixel = get_meters_per_pixel(
                        rel_altitude_m=telemetry["rel_alt"],
                        focal_length_mm=self.config.drone_args["true_focal_len_mm"],
                        sensor_width_mm=self.config.drone_args["sensor_width_mm"],
                        sensor_height_mm=self.config.drone_args["sensor_height_mm"],
                        sensor_width_pixels=self.config.drone_args["sensor_width_pixels"],
                        sensor_height_pixels=self.config.drone_args["sensor_height_pixels"],
                        image_width_pixels=frame_width,
                        image_height_pixels=frame_height,
                    )

                    safety_radius_pixels = int(
                        self.config.input_args["safety_radius_m"] / meters_per_pixel
                    )
                    logger.debug(
                        f"meters/pixel={meters_per_pixel:.4f}, "
                        f"radius_m={self.config.input_args['safety_radius_m']}, "
                        f"radius_px={safety_radius_pixels}"
                    )

                    # ============== COMPUTE LOCATION (LNG,LAT) OF FRAME CORNERS  ===================================
                    # get the coordinates of the 4 corners of the frame.
                    # The rectangle may be oriented in any direction wrt North
                    corners_coordinates = get_objects_coordinates(
                        objects_coords=frame_corners,
                        center_lat=telemetry["latitude"],
                        center_lon=telemetry["longitude"],
                        frame_width_pixels=frame_width,
                        frame_height_pixels=frame_height,
                        meters_per_pixel=meters_per_pixel,
                        angle_wrt_north=telemetry["gb_yaw"],
                    )

                    # ============== CREATE TRANSFORM TO EXTRACT THE FRAME AREA FROM THE RASTER ========================
                    # compute the Affine transform to extract a portion of the raster corresponding to the area in the frame
                    frame_transform = get_frame_transform(
                        height=frame_height,
                        width=frame_width,
                        drone_ul=tuple(corners_coordinates[0]),  # (lon, lat) for upper-left corner
                        drone_ur=tuple(corners_coordinates[1]),  # (lon, lat) for upper-right corner
                        drone_bl=tuple(corners_coordinates[3]),  # (lon, lat) for bottom-left corner
                    )

                    # ============== EXACT-MATCH CACHE  ===================================
                    # If telemetry is identical to the previous frame, all geo masks are
                    # guaranteed to be identical — skip every geo computation.
                    tel_key = (
                        telemetry["latitude"],
                        telemetry["longitude"],
                        telemetry["rel_alt"],
                        telemetry["gb_yaw"],
                    )

                    if tel_key == prev_tel_key:
                        nodata_dem_mask = prev_nodata_mask
                        slope_mask      = prev_slope_mask
                        geofencing_mask = prev_geofencing_mask
                        logger.debug(f"Frame {meta.frame_id}: exact telemetry match — reusing previous geo masks.")

                    else:
                        # ============== DEM MASKS (with window cache)  ===========================
                        if dem_tif is not None:
                            center_coords = (telemetry["longitude"], telemetry["latitude"])

                            if _footprint_within_bounds(corners_coordinates, dem_cache_bounds):
                                # Cache hit: frame footprint is still inside the previously
                                # extracted oversized DEM window — skip I/O and slope computation.
                                logger.debug(f"Frame {meta.frame_id}: DEM window cache hit.")
                            else:
                                # Cache miss: extract an oversized window, compute slope once,
                                # store both masks at window resolution for subsequent frames.
                                logger.debug(f"Frame {meta.frame_id}: DEM window cache miss — re-extracting.")
                                dem_window, dem_mask_window, dem_window_transform, dem_window_bounds, dem_window_size = extract_dem_window(
                                    dem_tif=dem_tif,
                                    dem_mask_tif=dem_mask_tif,
                                    center_lonlat=center_coords,
                                    rectangle_lonlat=corners_coordinates,
                                    buffer_scale=self.config.dem_cache_buffer_scale,
                                ) # masks shape are (1,window_size, window_size)

                                # find the distance in meters between two points on opposite sides of
                                # the window at the drone latitude, then derive the DEM pixel size
                                dem_window_size_m = get_window_size_m(telemetry["latitude"], dem_window_bounds)
                                dem_pixel_size_m  = dem_window_size_m / dem_window_size

                                # ============== COMPUTE SLOPE MASK FROM DEM WINDOW ===================================
                                # compute the slope mask using the dem window and the resolution of each pixel
                                slope_mask_window = compute_slope_mask_horn(
                                    elev_array=dem_window,
                                    pixel_size=dem_pixel_size_m,
                                    slope_threshold_deg=self.config.input_args["slope_angle_threshold"],
                                )

                                # stack the dem_nodata and dem_slope masks into a (2, W, W) array and cache
                                dem_cache_masks_window     = np.concatenate((dem_mask_window, slope_mask_window), axis=0)
                                dem_cache_window_transform = dem_window_transform
                                dem_cache_bounds           = dem_window_bounds

                            # ============== ROTATE & UPSCALE MASKS USING FRAME TRANSFORM ========================
                            # rotate and resample the cached window masks using the frame corner coordinates
                            # to obtain a (frame_height, frame_width) version that aligns with the drone frame
                            combined = map_window_onto_drone_frame(
                                window=dem_cache_masks_window,
                                window_transform=dem_cache_window_transform,
                                dst_transform=frame_transform,
                                output_shape=(2, frame_height, frame_width),
                                crs=dem_tif.crs,
                            )
                            # separate the two masks
                            nodata_dem_mask = combined[0]
                            slope_mask      = combined[1]

                        else:
                            nodata_dem_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
                            slope_mask      = np.zeros((frame_height, frame_width), dtype=np.uint8)

                        # ============== CREATE GEOFENCING MASK ========================
                        # compute geofencing directly on the full size frame, independently
                        # of the DEM masks. Does not require DEM data, but does require
                        # known drone lat/lon to build the frame transform.
                        if geofencing_polygon is not None:
                            geofencing_mask = create_geofencing_mask_runtime(
                                frame_width=frame_width,
                                frame_height=frame_height,
                                transform=frame_transform,
                                polygon=geofencing_polygon,
                            )
                        else:
                            geofencing_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)

                        # Update exact-match cache.
                        prev_tel_key         = tel_key
                        prev_nodata_mask     = nodata_dem_mask
                        prev_slope_mask      = slope_mask
                        prev_geofencing_mask = geofencing_mask

                # ---- write frame and all masks directly into the output slot ----
                out_slot = self.output_frame_buffer.acquire()
                if out_slot is None:
                    self.input_frame_buffer.release(meta.slot_index)
                    logger.warning(
                        f"No free slot in output frame buffer. "
                        f"Frame {meta.frame_id} discarded. Consumer too slow?"
                    )
                    continue

                frame_dst, mask_dst = self.output_frame_buffer.view(out_slot)
                np.copyto(frame_dst, frame_view)
                mask_dst[0] = seg_mask_view[0]   # roads_mask
                mask_dst[1] = seg_mask_view[1]   # vehicles_mask
                self.input_frame_buffer.release(meta.slot_index)
                mask_dst[2] = nodata_dem_mask
                mask_dst[3] = geofencing_mask
                mask_dst[4] = slope_mask

                # ---- build and enqueue output metadata ----
                out_meta = GeoSlotMetadata(
                    frame_id=meta.frame_id,
                    timestamp=meta.timestamp,
                    original_wh=meta.original_wh,
                    slot_index=out_slot,
                    telemetry=meta.telemetry,
                    classes_names=meta.classes_names,
                    num_classes=meta.num_classes,
                    classes=meta.classes,
                    boxes_centers=meta.boxes_centers,
                    boxes_corner1=meta.boxes_corner1,
                    boxes_corner2=meta.boxes_corner2,
                    safety_radius_pixels=safety_radius_pixels,
                )

                append_start = time()
                try:
                    self.output_meta_queue.put(out_meta, timeout=self.config.queue_timeout)
                    logger.debug(
                        f"Frame {meta.frame_id} → output slot {out_slot}, "
                        f"geo results queued."
                    )
                except QueueFullException:
                    self.output_frame_buffer.release(out_slot)
                    logger.error(
                        f"Output metadata queue full. Frame {meta.frame_id} discarded. "
                        "Consumer too slow or stopped?"
                    )

                iter_end = time()

                logger.debug(
                    f"frame {meta.frame_id} processed in {(iter_end - iter_start) * 1000:.2f} ms, "
                    f"of which --> "
                    f"GET: {(predict_start - get_start) * 1000:.2f} ms, "
                    f"PREDICT: {(append_start - predict_start) * 1000:.2f} ms, "
                    f"PROPAGATE: {(iter_end - append_start) * 1000:.2f} ms."
                )
                # iteration completed correctly, move on to process next frame

            # Propagate termination signal via poison pill on clean shutdown.
            if not self.error_event.is_set():
                try:
                    logger.info("Attempting to put sentinel value on output queue ...")
                    self.output_meta_queue.put(POISON_PILL, timeout=self.config.poison_pill_timeout)
                    logger.info("Sentinel value has been passed on to the next process.")
                except Exception as e:
                    logger.error(f"Error propagating Poison Pill: {e}")
                    self.error_event.set()
                    logger.warning(
                        "Error event set: force-stop application since downstream processes "
                        "are unable to receive the poison pill."
                    )
            else:
                logger.info("Terminating and skipping Poison Pill sending. Error event is set.")

        except Exception as e:
            logger.critical(f"An unexpected critical error happened in the Geo process: {e}", exc_info=True)
            self.error_event.set()
            logger.warning("Error event set: force-stopping the application")

        finally:

            close_tifs([dem_tif, dem_mask_tif])
            self.input_frame_buffer.close()
            self.output_frame_buffer.close()

            logger.info(
                "Geo process terminated successfully. "
                f"Poison pill received: {poison_pill_received}. "
                f"Error event: {self.error_event.is_set()}."
            )
            self.work_finished.set()


if __name__ == "__main__":

    import threading

    STACKED_SEG_SHAPE = (720, 1280, 5)   # (H, W, frame+seg_masks) — input slot
    STACKED_GEO_SHAPE = (720, 1280, 8)   # (H, W, frame+seg+geo_masks) — output slot
    N_SLOTS = 3
    N_FRAMES = 10

    error_event = mp.Event()

    input_meta_queue = mp.Queue(maxsize=N_SLOTS)
    input_frame_buffer = FrameBuffer(frame_shape=STACKED_SEG_SHAPE, n_slots=N_SLOTS)

    output_meta_queue = mp.Queue(maxsize=N_SLOTS)
    output_frame_buffer = FrameBuffer(frame_shape=STACKED_GEO_SHAPE, n_slots=N_SLOTS)

    config = GeoWorkerConfig(
        input_args={
            "dem": None,
            "dem_mask": None,
            "safety_radius_m": 10.0,
            "slope_angle_threshold": 30.0,
            "geofencing_vertexes": None,
        },
        drone_args={
            "true_focal_len_mm": 4.5,
            "sensor_width_mm": 6.3,
            "sensor_height_mm": 4.7,
            "sensor_width_pixels": 4056,
            "sensor_height_pixels": 3040,
        },
    )

    worker = GeoWorker(
        input_meta_queue=input_meta_queue,
        input_frame_buffer=input_frame_buffer,
        output_meta_queue=output_meta_queue,
        output_frame_buffer=output_frame_buffer,
        error_event=error_event,
        config=config,
    )

    def producer_loop():
        """Push fake SegmentationSlotMetadata into the input shared memory buffer."""
        for i in range(N_FRAMES):
            slot = input_frame_buffer.acquire()
            if slot is not None:
                stacked = np.zeros(STACKED_SEG_SHAPE, dtype=np.uint8)
                input_frame_buffer.write(slot, stacked)
                meta = SegmentationSlotMetadata(
                    frame_id=i,
                    timestamp=time(),
                    original_wh=(1920, 1080),
                    slot_index=slot,
                    telemetry=None,
                    classes_names={0: "cow"},
                    num_classes=1,
                    classes=np.array([], dtype=np.int32),
                    boxes_centers=np.empty((0, 2), dtype=np.float32),
                    boxes_corner1=np.empty((0, 2), dtype=np.float32),
                    boxes_corner2=np.empty((0, 2), dtype=np.float32),
                )
                try:
                    input_meta_queue.put(meta, timeout=1.0)
                except Exception:
                    input_frame_buffer.release(slot)
            sleep(1 / 10)
        input_meta_queue.put(POISON_PILL)

    def consumer_loop():
        """Drain the output queue and release output slots."""
        frames_received = 0
        while True:
            try:
                msg = output_meta_queue.get(timeout=10.0)
            except Exception:
                print("[Consumer] Timed out. Stopping.")
                break
            if isinstance(msg, str) and msg == POISON_PILL:
                output_meta_queue.put(POISON_PILL)
                print(f"[Consumer] Poison pill received. {frames_received} frames processed.")
                break
            if error_event.is_set():
                break
            assert isinstance(msg, GeoSlotMetadata)
            stacked = output_frame_buffer.read(msg.slot_index)
            output_frame_buffer.release(msg.slot_index)
            frames_received += 1
            print(
                f"[Consumer] frame_id={msg.frame_id} "
                f"safety_radius_px={msg.safety_radius_pixels} "
                f"stacked_shape={stacked.shape} "
                f"slot={msg.slot_index}"
            )

    prod_thread = threading.Thread(target=producer_loop, daemon=True)
    cons_thread = threading.Thread(target=consumer_loop, daemon=True)

    print("[Main] Starting worker ...")
    worker.start()
    sleep(0.5)

    print("[Main] Starting consumer ...")
    cons_thread.start()

    print("[Main] Starting producer ...")
    prod_thread.start()

    worker.join()
    prod_thread.join(timeout=5.0)
    cons_thread.join(timeout=5.0)

    input_frame_buffer.unlink()
    output_frame_buffer.unlink()
    print("[Main] Done.")
