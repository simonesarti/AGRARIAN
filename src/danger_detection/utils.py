import logging
from pathlib import Path
from rasterio.warp import reproject, Resampling
from rasterio.transform import rowcol
from rasterio.windows import bounds
from affine import Affine
import rasterio
from rasterio.features import rasterize
from rasterio import RasterioIOError

import cv2
import numpy as np
from geopy.distance import geodesic

from typing import Optional, Union, Tuple

_logger = logging.getLogger(__name__)

__all__ = [
    "open_dem_tifs",
    "close_tifs",
    "extract_dem_window",
    "get_window_size_m",
    "compute_slope_mask_runtime",
    "compute_slope_mask_horn",
    "create_geofencing_mask_runtime",
    "get_frame_transform",
    "map_window_onto_drone_frame",
    "create_dangerous_intersections_masks",
    "create_safety_mask",
]


def safe_open_raster(path: Optional[Union[str, Path]]) -> Optional[rasterio.DatasetReader]:
    """Helper function to handle the repetitive opening logic."""
    if path is None:
        return None

    path_obj = Path(path)
    if not path_obj.exists():
        print(f"File not found at: {path_obj}")
        return None

    try:
        return rasterio.open(path_obj)
    except (RasterioIOError, OSError) as e:
        print(f"Failed to open {path_obj.name}: {e}")
        return None


def open_dem_tifs(
        dem_path: Optional[Union[str, Path]],
        dem_mask_path: Optional[Union[str, Path]]
) -> Tuple[Optional[rasterio.DatasetReader], Optional[rasterio.DatasetReader]]:

    # Open the primary DEM
    dem_tif = safe_open_raster(dem_path)

    # 2. Open the mask (only if DEM exists and was opened successfully)
    dem_mask_tif = None
    if dem_tif and dem_mask_path:
        dem_mask_tif = safe_open_raster(dem_mask_path)

    return dem_tif, dem_mask_tif


def close_tifs(tif_files):
    """
    Closes all non-None open TIFF files in the given list.

    Args:
        tif_files (list): A list of file objects or None values.
    """
    for tif in tif_files:
        if tif is not None and not tif.closed:
            tif.close()


def extract_dem_window(dem_tif, dem_mask_tif, center_lonlat, rectangle_lonlat, buffer_scale: float = 0.5):
    """
    Extracts a square window from a raster that fully encompasses a rotated rectangle.

    Args:
        dem_tif (rasterio.DatasetReader): Opened raster dataset.
        dem_mask_tif (rasterio.DatasetReader): Opened raster mask dataset.
        center_lonlat (tuple): (longitude, latitude) of the center point.
        rectangle_lonlat (numpy.ndarray): (4,2) array of (longitude, latitude) rectangle corners.

    """
    # --- Step 1 & 2: Convert center and rectangle corners to pixel coordinates in one call ---
    transform = dem_tif.transform
    all_xs = np.array([center_lonlat[0], *rectangle_lonlat[:, 0]])
    all_ys = np.array([center_lonlat[1], *rectangle_lonlat[:, 1]])
    rows, cols = rowcol(transform=transform, xs=all_xs, ys=all_ys)
    center_y, center_x = rows[0], cols[0]
    rows_corners, cols_corners = rows[1:], cols[1:]

    # --- Step 3: Compute square window size (odd, centered on drone position) ---
    # Use the longest side of the corners' bounding box rather than the circumradius
    # to avoid a sqrt.  buffer_scale is clamped to >= 0.5 so the window is always
    # at least double the longest side, guaranteeing the rotated frame fits inside.
    longest_side = max(
        int(rows_corners.max() - rows_corners.min()),
        int(cols_corners.max() - cols_corners.min()),
        1,  # guard: entire frame fits in one DEM pixel (very low altitude / coarse DEM)
    )
    # ensure buffer large enough to allow frame to fit in DEM window fits the DEM even after 45 deg rotation
    effective_scale = max(buffer_scale, 0.5)    
    half_size = int(np.ceil(longest_side * (0.5 + effective_scale)))
    # ensure window size is odd to have a center pixel
    window_size = 2 * half_size + 1 

    # --- Step 4: Define the window in pixel coordinates ---
    window_row_start = center_y - half_size
    window_col_start = center_x - half_size

    window_row_end = center_y + half_size
    window_col_end = center_x + half_size

    # center in indexes (row=9, col=6), half size =3
    # => window_row_start = 6 ... |6|7|8| X |10|11|12
    # => window_col_start = 3 ... |3|4|5| X |7 |8 |9

    # --- Step 5 & 6: Compute geographic reference, check bounds, extract data ---
    
    # rasterio behaviour (confirmed empirically):
    #   - window_transform() and bounds() are pure affine arithmetic on the Window's
    #     col_off/row_off/width/height. They never clamp to the raster extent, so they
    #     always return the correct geographic reference for the full requested area,
    #     even when part of it lies outside the raster.
    #   - read() on a partially-OOB window silently clips to the intersection with the
    #     raster and returns a SMALLER array than the requested window size. This does
    #     not affect bounds() — the two operations are independent.
    window = rasterio.windows.Window(
        col_off=window_col_start,
        row_off=window_row_start,
        width=window_size,
        height=window_size,
    )
    window_transform = dem_tif.window_transform(window)
    window_bounds    = bounds(window, dem_tif.transform)

    within_bounds = (
        window_col_start >= 0 and
        window_row_start >= 0 and
        window_col_end < dem_tif.width and
        window_row_end < dem_tif.height
    )

    if within_bounds:
        dem_window_array = dem_tif.read(window=window)
        if dem_mask_tif is not None:
            dem_mask_window_array = dem_mask_tif.read(window=window)
        else:
            # if no DEM validity mask provided, assume all DEM data is safe (mask=0)
            dem_mask_window_array = np.zeros((1, window_size, window_size), dtype=np.uint8)

    else:
        # The drone is near or outside the DEM boundary.
        # rasterio.read() silently clips to the intersection with the raster, so we
        # pass the same unclamped window and use the returned shape to embed the valid
        # strip at the correct position in the full-sized output arrays.
        #   - DEM elevation: zero-filled outside valid area
        #   - nodata mask:   1 (= nodata) outside valid area, so downstream danger
        #                    detection treats those pixels as hazardous
        _logger.warning(
            "Requested DEM window extends outside the raster bounds — "
            "out-of-bounds pixels will be marked as nodata. "
            f"Window rows [{window_row_start}, {window_row_end}] vs DEM height {dem_tif.height}, "
            f"cols [{window_col_start}, {window_col_end}] vs DEM width {dem_tif.width}."
        )

        # Embedding offsets: how far into the full output array the valid data starts.
        # max(0, -start) handles the case where the window starts before the raster edge.
        row_off = max(0, -window_row_start)
        col_off = max(0, -window_col_start)

        valid_dem = dem_tif.read(window=window)   # clipped by rasterio, shape (1, valid_h, valid_w)
        dem_window_array = np.zeros((1, window_size, window_size), dtype=valid_dem.dtype)
        dem_window_array[:, row_off:row_off + valid_dem.shape[1], col_off:col_off + valid_dem.shape[2]] = valid_dem

        # All pixels start as nodata=1; valid region is overwritten from the mask tif
        dem_mask_window_array = np.ones((1, window_size, window_size), dtype=np.uint8)
        if dem_mask_tif is not None:
            valid_mask = dem_mask_tif.read(window=window)
        else:
            # if no DEM validity mask provided, assume all DEM data is safe (mask=0)
            valid_mask = np.zeros_like(valid_dem, dtype=np.uint8)
        dem_mask_window_array[:, row_off:row_off + valid_mask.shape[1], col_off:col_off + valid_mask.shape[2]] = valid_mask

    return dem_window_array, dem_mask_window_array, window_transform, window_bounds, window_size


def get_window_size_m(reference_lat, window_bounds):
    (min_lon, min_lat, max_lon, max_lat) = window_bounds
    assert min_lat < reference_lat < max_lat

    # points for geopy must be in form (lat,long)
    point1 = (reference_lat, min_lon)
    point2 = (reference_lat, max_lon)
    distance_m = geodesic(point1, point2).meters

    return distance_m


def compute_slope_mask_horn(elev_array, pixel_size, slope_threshold_deg):
    """
    Compute a mask indicating where the terrain slope is steeper than a given threshold
    using Horn's method with 1-pixel edge padding. The input is a 3D array of elevation values 
    (shape: (1, H, W)) and the output is a 3D binary mask of the same shape.

    Parameters
    ----------
    elev_array : np.ndarray
        A 3D array (shape (1, H, W)) of elevation values in meters.
    pixel_size : float
        The physical size of each pixel in meters.
    slope_threshold_deg : float
        The slope threshold in degrees. Cells with a slope greater than this threshold
        will be marked with a 1 in the output mask.

    Returns
    -------
    np.ndarray
        A 3D binary array (shape (1, H, W)) with 1 where the computed slope exceeds 
        slope_threshold_deg, and 0 elsewhere.
    """
    # Ensure the input has the expected shape and remove the singleton dimension.
    assert elev_array.ndim == 3 and elev_array.shape[0] == 1, "Input must be of shape (1, H, W)"

    # arctan(sqrt(dx²+dy²)) is always in [0°, 90°), so no pixel can ever exceed
    # a threshold of 90° or more. Guard here because tan(θ) is negative for
    # θ ∈ (90°, 270°): squaring would produce a spuriously plausible positive
    # tan_threshold_sq and flip large portions of the mask incorrectly.
    if slope_threshold_deg >= 90.0:
        return np.zeros_like(elev_array, dtype=np.uint8)

    elev_array = elev_array[0]

    # Define Horn's kernels for x and y gradients
    kernel_x = np.array([[-1, 0, 1],
                         [-2, 0, 2],
                         [-1, 0, 1]], dtype=elev_array.dtype) / (8 * pixel_size)

    kernel_y = np.array([[-1, -2, -1],
                         [0, 0, 0],
                         [1, 2, 1]], dtype=elev_array.dtype) / (8 * pixel_size)

    # cv2.filter2D (cross-correlation, BORDER_REPLICATE) is ~2× faster than
    # scipy.ndimage.convolve on float32.  The sign of dx/dy is negated vs scipy
    # (correlation vs convolution), but dx²+dy² is unchanged so the mask is identical.
    dx = cv2.filter2D(elev_array, -1, kernel_x, borderType=cv2.BORDER_REPLICATE)
    dy = cv2.filter2D(elev_array, -1, kernel_y, borderType=cv2.BORDER_REPLICATE)

    # slope > threshold_deg  ↔  dx²+dy² > tan(threshold_rad)²
    # Both arctan and sqrt are strictly monotone and dx²+dy² ≥ 0, so the
    # threshold can be pre-squared once, eliminating three per-pixel
    # transcendental passes (sqrt, arctan, degrees).
    tan_threshold_sq = np.tan(np.radians(slope_threshold_deg)) ** 2
    mask = (dx ** 2 + dy ** 2 > tan_threshold_sq).astype(np.uint8)

    return mask[np.newaxis, :, :]


def create_geofencing_mask_runtime(frame_width, frame_height, transform, polygon):

    # Use rasterio.features.rasterize to create an array of shape (H, W)
    # The inside of the polygon will be set to 0 (OK)
    # The inside of the polygon will be set to 1 (OUT OF BOUNDS)
    mask = rasterize(
        [(polygon, 0)],  # list of (geometry, value)
        out_shape=(frame_height, frame_width),
        transform=transform,
        fill=1,
        dtype=np.uint8
    )

    return mask


def get_frame_transform(
        height,
        width,
        drone_ul,  # (lon, lat) for upper-left
        drone_ur,  # (lon, lat) for upper-right
        drone_bl,  # (lon, lat) for bottom-left
):

    # Build dst_transform using the known drone frame corners.
    # Here, we assume:
    #  (0, 0)             --> drone_ul
    #  (width-1, 0)        --> drone_ur
    #  (0, height-1)       --> drone_bl
    #

    # Build the affine transform.
    # In Rasterio, the transform maps (col, row) to (x, y) as:
    #    x = a * col + b * row + c
    #    y = d * col + e * row + f

    c, f = drone_ul  # The translation (c, f) is just the UL coordinate
    a = (drone_ur[0] - drone_ul[0]) / (width - 1)  # change in x per column
    d = (drone_ur[1] - drone_ul[1]) / (width - 1)  # change in y per column
    b = (drone_bl[0] - drone_ul[0]) / (height - 1)  # change in x per row
    e = (drone_bl[1] - drone_ul[1]) / (height - 1)  # change in y per row

    dst_transform = Affine(a, b, c, d, e, f)
    return dst_transform


def map_window_onto_drone_frame(
        window,
        window_transform,
        dst_transform,
        output_shape=(2, 1080, 1920),
        crs='EPSG:4326'
):
    """
    Map the DEM window to the drone frame using the output transform dst_transform,
    which is built from the provided drone frame corner coordinates.
    """
    # Reproject DEM into the output frame using dst_transform directly.
    out_array = np.empty(output_shape, dtype=window.dtype)
    reproject(
        source=window,
        destination=out_array,
        src_transform=window_transform,
        src_crs=crs,
        dst_transform=dst_transform,
        dst_crs=crs,
        resampling=Resampling.nearest
    )

    return out_array


def create_dangerous_intersections_masks(
    frame_height,
    frame_width,
    boxes_centers,
    safety_radius_pixels,
    segment_roads_danger_mask,
    segment_vehicles_danger_mask,
    dem_nodata_danger_mask,
    geofencing_danger_mask,
    slope_danger_mask,
):
    # pre-allocate danger type list
    danger_types = []
    
    # Combined danger mask: single-pass bitwise OR chain
    combined_danger = (
        segment_roads_danger_mask      |
        segment_vehicles_danger_mask   |
        dem_nodata_danger_mask         |
        geofencing_danger_mask         |
        slope_danger_mask
    ).astype(np.uint8)

    # Early exit: no valid safety radius or no boxes means no animals or no telemetry.
    # There cannot be an intersection if there is no safety area
    if safety_radius_pixels <= 0 or len(boxes_centers) == 0:
        return combined_danger, np.zeros((frame_height, frame_width), dtype=np.uint8), danger_types

    # create safety mask
    safety_mask = create_safety_mask(frame_height, frame_width, boxes_centers, safety_radius_pixels)

    # Combined intersection: safety circles overlapping any danger layer.
    combined_intersections = (safety_mask & combined_danger).astype(np.uint8)

    # Per-danger checks
    if combined_intersections.any():
        if (safety_mask & segment_roads_danger_mask).any():
            danger_types.append("Roads")
        if (safety_mask & segment_vehicles_danger_mask).any():
            danger_types.append("Vehicles")
        if (safety_mask & dem_nodata_danger_mask).any():
            danger_types.append("Missing elevation data")
        if (safety_mask & geofencing_danger_mask).any():
            danger_types.append("Out of geo-fenced area")
        if (safety_mask & slope_danger_mask).any():
            danger_types.append("Steep slope")

    combined_danger_no_intersections = combined_danger - combined_intersections

    return combined_danger_no_intersections, combined_intersections, danger_types


def create_safety_mask(frame_height, frame_width, boxes_centers, safety_radius = -1):
    # Initialize the mask with zeros
    safety_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)

    # safety radius is 0 or negative when the radius cannot be determined, so the safety masks cannot be drawn
    if safety_radius > 0:
        # Draw circles on the mask
        for box_center in boxes_centers:
            cv2.circle(safety_mask, box_center, safety_radius, 1, cv2.FILLED)

    return safety_mask




