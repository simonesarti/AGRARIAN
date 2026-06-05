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
from scipy.ndimage import convolve

from typing import Optional, Union, Tuple


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


def extract_dem_window(dem_tif, dem_mask_tif, center_lonlat, rectangle_lonlat):
    """
    Extracts a square window from a raster that fully encompasses a rotated rectangle.

    Args:
        dem_tif (rasterio.DatasetReader): Opened raster dataset.
        dem_mask_tif (rasterio.DatasetReader): Opened raster mask dataset.
        center_lonlat (tuple): (longitude, latitude) of the center point.
        rectangle_lonlat (numpy.ndarray): (4,2) array of (longitude, latitude) rectangle corners.

    """
    # --- Step 1: Convert center point to pixel coordinates ---
    transform = dem_tif.transform
    center_y, center_x = rowcol(transform=transform, xs=center_lonlat[0], ys=center_lonlat[1])

    # --- Step 2: Convert rectangle corners to pixel coordinates ---
    pixel_coords_yx = np.array([rowcol(transform=transform, xs=lon, ys=lat) for lon, lat in rectangle_lonlat])

    # Compute the maximum pixel distance from the center
    pixel_dists = np.linalg.norm(pixel_coords_yx - np.array([center_y, center_x]), axis=1)
    max_dist = int(np.max(pixel_dists))  # Maximum pixel distance

    # --- Step 3: Compute square window size (odd number with buffer) ---
    buffer = int(np.ceil(max_dist * 0.5))  # Extra space for rotation
    half_size = max_dist + buffer
    window_size = 2 * half_size + 1  # Ensure window is odd

    # --- Step 4: Define the window in pixel coordinates ---
    window_row_start = center_y - half_size
    window_col_start = center_x - half_size

    window_row_end = center_y + half_size
    window_col_end = center_x + half_size

    # center in indexes (row=9, col=6), half size =3
    # => window_row_start = 6 ... |6|7|8| X |10|11|12
    # => window_col_start = 3 ... |3|4|5| X |7 |8 |9

    # --- Step 5: Make sure the window is inside the tif ---
    if (
            window_col_start < 0 or
            window_row_start < 0 or
            window_col_end >= dem_tif.width or
            window_row_end >= dem_tif.height
    ):
        print(f"ERROR: Cannot monitor the safety of animals when the drones is leaving the DEM area")
        print(f"DEM rows: {dem_tif.height}")
        print(f"DEM window rows: [{window_row_start}, {window_row_start + window_size}]")
        print(f"DEM columns: {dem_tif.width}")
        print(f"DEM window columns: [{window_col_start}, {window_col_start + window_size}]")
        exit()

    # --- Step 6: Extract the window from the raster ---
    window = rasterio.windows.Window(col_off=window_col_start, row_off=window_row_start, width=window_size, height=window_size)
    window_transform = dem_tif.window_transform(window)

    # Read the dem window from the raster
    dem_window_array = dem_tif.read(window=window)

    # Read the dem window from the raster (if None, assume mask alla values are valid)
    if dem_mask_tif is not None:
        dem_mask_window_array = dem_mask_tif.read(window=window)
    else:
        dem_mask_window_array = np.zeros((1, window_size, window_size), dtype=np.uint8)

    # get the bounds of the window
    window_bounds = bounds(window, dem_tif.transform)

    return dem_window_array, dem_mask_window_array, window_transform, window_bounds, window_size


def get_window_size_m(reference_lat, window_bounds):
    (min_lon, min_lat, max_lon, max_lat) = window_bounds
    assert min_lat < reference_lat < max_lat

    # points for geopy must be in form (lat,long)
    point1 = (reference_lat, min_lon)
    point2 = (reference_lat, max_lon)
    distance_m = geodesic(point1, point2).meters

    return distance_m


def compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg):
    """
    Compute a mask indicating where the terrain slope is steeper than a given threshold.

    Parameters
    ----------
    elev_array : np.ndarray
        A square 3D array of elevation values in meters, with first dimension has shape 1.
    pixel_size : float
        The size of each pixel in meters.
    slope_threshold_deg : float
        The slope threshold in degrees. Cells with a slope greater than this threshold
        will be marked with a 1 in the output mask.

    Returns
    -------
    np.ndarray
        A 3D array (of the same shape as elev_array) containing 1 where the slope is
        greater than slope_threshold_deg and 0 elsewhere.
    """

    # If elev_array has a singleton first dimension, remove it to work with a 2D array.
    assert elev_array.ndim == 3 and elev_array.shape[0] == 1
    elev_array = elev_array[0]

    # Pad the DEM with a 1-pixel border using edge replication.
    elev_padded = np.pad(elev_array, pad_width=1, mode='edge')

    # Compute the gradient on the padded DEM.
    # np.gradient returns arrays of the same shape as the input.
    gy_padded, gx_padded = np.gradient(elev_padded, pixel_size)

    # Crop the gradient arrays to remove the padding.
    gx = gx_padded[1:-1, 1:-1]
    gy = gy_padded[1:-1, 1:-1]

    # Compute the magnitude of the slope (rise over run)
    # The slope in radians is given by arctan(sqrt((dz/dx)^2 + (dz/dy)^2)).
    slope_radians = np.arctan(np.sqrt(gx ** 2 + gy ** 2))

    # Convert the slope to degrees
    slope_degrees = np.degrees(slope_radians)

    # Create a mask where a cell is 1 if the slope exceeds the threshold, 0 otherwise.
    mask = (slope_degrees > slope_threshold_deg).astype(np.uint8)

    # Expand the dimensions to ensure the output is (1, W, H).
    mask = mask[np.newaxis, :, :]

    return mask


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
    elev_array = elev_array[0]

    # Define Horn's kernels for x and y gradients
    kernel_x = np.array([[-1, 0, 1],
                         [-2, 0, 2],
                         [-1, 0, 1]]) / (8 * pixel_size)

    kernel_y = np.array([[-1, -2, -1],
                         [0, 0, 0],
                         [1, 2, 1]]) / (8 * pixel_size)

    # Compute gradients using convolution with edge handling directly in convolve
    dx = convolve(elev_array, kernel_x, mode='nearest')
    dy = convolve(elev_array, kernel_y, mode='nearest')

    # Calculate slope in radians using Horn's formula
    slope_radians = np.arctan(np.sqrt(dx ** 2 + dy ** 2))

    # Convert to degrees
    slope_degrees = np.degrees(slope_radians)

    # Create mask where slope exceeds threshold
    # Extract the portion corresponding to the original array
    mask = (slope_degrees > slope_threshold_deg).astype(np.uint8)

    # Return the mask with the original 3D shape (1, H, W).
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




