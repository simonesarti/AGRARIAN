import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from pathlib import Path
from geopy.distance import geodesic
import cv2

from app.danger_detection.utils import *
from app.danger_detection.utils import merge_3d_mask


def create_temp_tif(tmp_path, filename="temp.tif", width=10, height=10, count=1, dtype='uint8'):
    """
    Helper function to create a minimal valid GeoTIFF file.
    Returns the path to the created file.
    """
    file_path = tmp_path / filename
    transform = from_origin(0, 10, 1, 1)  # arbitrary affine transform
    data = np.ones((height, width), dtype=dtype)
    # Write the data as a single band GeoTIFF
    with rasterio.open(
            file_path,
            'w',
            driver='GTiff',
            height=height,
            width=width,
            count=count,
            dtype=dtype,
            transform=transform,
    ) as dst:
        dst.write(data, 1)
    return file_path, data


def check_raster_content(tif, expected_data):
    """
    Helper function to check if a raster file contains the expected content.
    """
    with tif as dataset:
        # Read the first band
        read_data = dataset.read(1)
        # Check if the data matches the expected data
        np.testing.assert_array_equal(read_data, expected_data)


def test_get_dem_valid(tmp_path):
    """Test that get_dem returns an open dataset when provided a valid DEM file path."""
    dem_file, expected_data = create_temp_tif(tmp_path, "dem.tif")
    dem = get_dem(dem_file)
    # Verify that we got a dataset and that it is open.
    assert dem is not None
    assert not dem.closed
    # The 'name' attribute of the returned dataset should match the file path.
    assert Path(dem.name).resolve() == dem_file.resolve()

    # Check raster content
    check_raster_content(dem, expected_data)

    dem.close()


def test_get_dem_invalid(tmp_path):
    """Test that get_dem raises an error for a non-existent file."""
    non_existent = tmp_path / "nonexistent.tif"
    with pytest.raises(rasterio.errors.RasterioIOError):
        get_dem(non_existent)


def test_get_dem_mask_none():
    """Test that get_dem_mask returns None when given None as input."""
    dem_mask = get_dem_mask(None)
    assert dem_mask is None


def test_get_dem_mask_valid(tmp_path):
    """Test that get_dem_mask returns an open dataset when provided a valid mask file path."""
    mask_file, expected_data = create_temp_tif(tmp_path, "dem_mask.tif")
    dem_mask = get_dem_mask(mask_file)
    assert dem_mask is not None
    assert not dem_mask.closed
    assert Path(dem_mask.name).resolve() == mask_file.resolve()

    # Check raster content
    check_raster_content(dem_mask, expected_data)

    dem_mask.close()


def test_get_dem_mask_invalid(tmp_path):
    """Test that get_dem_mask raises an error when provided a non-existent file."""
    non_existent = tmp_path / "nonexistent_mask.tif"
    with pytest.raises(rasterio.errors.RasterioIOError):
        get_dem_mask(non_existent)


def test_close_tifs(tmp_path):
    """Test that close_tifs correctly closes open TIFF datasets."""
    # Create two valid TIFFs.
    dem_file, _ = create_temp_tif(tmp_path, "dem.tif")
    mask_file, _ = create_temp_tif(tmp_path, "dem_mask.tif")
    dem = get_dem(dem_file)
    dem_mask = get_dem_mask(mask_file)
    # Include an explicit None in the list.
    tif_list = [dem, dem_mask, None]

    # Verify that datasets are open before closing.
    assert not dem.closed
    assert not dem_mask.closed

    # Call the function that should close these datasets.
    close_tifs(tif_list)

    # Verify that both datasets are now closed.
    assert dem.closed
    assert dem_mask.closed


def test_close_already_closed(tmp_path):
    """Test that close_tifs safely handles datasets that are already closed."""
    file_path, _ = create_temp_tif(tmp_path, "dem.tif")
    dem = get_dem(file_path)
    dem.close()  # Manually close the dataset.
    # Pass the already closed dataset to close_tifs; it should not raise an error.
    close_tifs([dem])
    assert dem.closed


def test_close_empty_list():
    """Test that calling close_tifs with an empty list does nothing (and raises no error)."""
    close_tifs([])


@pytest.mark.parametrize("mask_3d, expected", [
    # Test case 1: All zeros -> should return a mask of all zeros.
    (np.zeros((3, 5, 5), dtype=np.uint8),
     np.zeros((5, 5), dtype=np.uint8)),

    # Test case 2: All ones -> should return a mask of all ones.
    (np.ones((3, 5, 5), dtype=np.uint8),
     np.ones((5, 5), dtype=np.uint8)),

    # Test case 3: Mixed zeros and ones.
    # Here the expected result is computed as the logical or along the channel axis.
    (
            np.array([[[0, 1, 0],
                       [1, 0, 0],
                       [0, 0, 0]],

                      [[0, 0, 1],
                       [0, 0, 0],
                       [1, 0, 0]],

                      [[0, 0, 0],
                       [0, 1, 0],
                       [0, 0, 1]]], dtype=np.uint8),

            np.array([[0, 1, 1],
                      [1, 1, 0],
                      [1, 0, 1]], dtype=np.uint8)
    )
])
def test_merge_3d_mask(mask_3d, expected):
    # Call the function under test.
    result = merge_3d_mask(mask_3d)

    # Check that the resulting mask has the correct shape.
    assert result.shape == expected.shape, f"Expected shape {expected.shape}, got {result.shape}"

    # Check that the resulting mask matches the expected mask.
    np.testing.assert_array_equal(result, expected)


def test_get_window_size_valid_equator():
    """
    Test with reference latitude at the equator.
    Window bounds are defined such that reference_lat (0) is between min_lat and max_lat.
    """
    reference_lat = 0.0
    # Define window_bounds as (min_lon, min_lat, max_lon, max_lat)
    window_bounds = (-1.0, -1.0, 1.0, 1.0)
    expected_distance = geodesic((reference_lat, -1.0), (reference_lat, 1.0)).meters

    distance = get_window_size_m(reference_lat, window_bounds)
    assert distance == pytest.approx(expected_distance)


def test_get_window_size_valid_mid_lat():
    """
    Test with a mid-latitude value.
    """
    reference_lat = 45.0
    window_bounds = (-120.0, 30.0, -110.0, 60.0)  # 45.0 is between 30 and 60.
    expected_distance = geodesic((reference_lat, -120.0), (reference_lat, -110.0)).meters

    distance = get_window_size_m(reference_lat, window_bounds)
    assert distance == pytest.approx(expected_distance)


@pytest.mark.parametrize("reference_lat, window_bounds", [
    # reference_lat is below the min_lat.
    (-2.0, (-1.0, -1.0, 1.0, 1.0)),
    # reference_lat is above the max_lat.
    (2.0, (-1.0, -1.0, 1.0, 1.0)),
    # reference_lat equals the minimum latitude.
    (-1.0, (-1.0, -1.0, 1.0, 1.0)),
    # reference_lat equals the maximum latitude.
    (1.0, (-1.0, -1.0, 1.0, 1.0)),
])
def test_get_window_size_invalid_reference_lat(reference_lat, window_bounds):
    """
    Test that the function raises an AssertionError when the reference latitude is not
    strictly between min_lat and max_lat.
    """
    with pytest.raises(AssertionError):
        get_window_size_m(reference_lat, window_bounds)


def test_create_safety_mask_empty():
    """
    Test that when boxes_centers is empty,
    the returned safety mask is an array of zeros.
    """
    frame_height, frame_width = 100, 100
    boxes_centers = np.array([], dtype=int)  # Empty array of centers
    safety_radius = 10

    mask = create_safety_mask(frame_height, frame_width, boxes_centers, safety_radius)
    expected = np.zeros((frame_height, frame_width), dtype=np.uint8)
    np.testing.assert_array_equal(mask, expected)


def test_create_safety_mask_single_center():
    """
    Test that when a single center is provided,
    a filled circle is drawn in the mask at the expected location.
    """
    frame_height, frame_width = 50, 50
    boxes_centers = np.array([[25, 25]])  # One center at (25, 25)
    safety_radius = 5

    mask = create_safety_mask(frame_height, frame_width, boxes_centers, safety_radius)

    # Manually compute expected mask.
    expected = np.zeros((frame_height, frame_width), dtype=np.uint8)
    # cv2.circle expects the center as a tuple.
    cv2.circle(expected, (25, 25), safety_radius, 1, cv2.FILLED)

    np.testing.assert_array_equal(mask, expected)


def test_create_safety_mask_multiple_centers():
    """
    Test that when multiple centers are provided,
    circles are drawn for each center and merged correctly.
    """
    frame_height, frame_width = 100, 100
    # Two centers at different locations.
    boxes_centers = np.array([[30, 30], [70, 70]])
    safety_radius = 10

    mask = create_safety_mask(frame_height, frame_width, boxes_centers, safety_radius)

    expected = np.zeros((frame_height, frame_width), dtype=np.uint8)
    cv2.circle(expected, (30, 30), safety_radius, 1, cv2.FILLED)
    cv2.circle(expected, (70, 70), safety_radius, 1, cv2.FILLED)

    np.testing.assert_array_equal(mask, expected)


def test_no_detections_no_danger():
    """
    Test case where there are no boxes and no danger masks;
    all outputs should be zero.
    """
    frame_height, frame_width = 100, 100
    boxes_centers = np.empty((0, 2), dtype=int)  # No detections
    safety_radius_pixels = 10

    # All danger masks are zero
    segment_danger_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
    dem_nodata_danger_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
    geofencing_danger_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
    slope_danger_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)

    combined_danger_mask, combined_intersections, danger_types = create_dangerous_intersections_masks(
        frame_height,
        frame_width,
        boxes_centers,
        safety_radius_pixels,
        segment_danger_mask,
        dem_nodata_danger_mask,
        geofencing_danger_mask,
        slope_danger_mask
    )

    # Expect all outputs to be zero
    assert np.all(combined_danger_mask == 0)
    assert np.all(combined_intersections == 0)
    assert danger_types == []


def test_single_danger_type():
    """Test a scenario where only one type of danger is present inside the safety zone."""
    frame_height, frame_width = 100, 100
    boxes_centers = np.array([[50, 50]])  # A single detection
    safety_radius_pixels = 10

    # Create safety mask
    safety_mask = create_safety_mask(frame_height, frame_width, boxes_centers, safety_radius_pixels)

    # Only segment_danger_mask has danger inside safety
    segment_danger_mask = safety_mask.copy()
    dem_nodata_danger_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
    geofencing_danger_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
    slope_danger_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)

    combined_danger_mask, combined_intersections, danger_types = create_dangerous_intersections_masks(
        frame_height,
        frame_width,
        boxes_centers,
        safety_radius_pixels,
        segment_danger_mask,
        dem_nodata_danger_mask,
        geofencing_danger_mask,
        slope_danger_mask
    )

    # Expect intersection only where safety meets segment danger
    assert np.any(combined_intersections > 0)
    assert danger_types == ["Vehicles Danger"]
    assert np.all((combined_danger_mask + combined_intersections) <= 1)  # Ensure binary mask
    assert np.all(0 <= (combined_danger_mask + combined_intersections))  # Ensure binary mask


def test_multiple_danger_types():
    """Test a scenario where multiple types of dangers intersect with the safety zone."""
    frame_height, frame_width = 100, 100
    boxes_centers = np.array([[30, 30], [70, 70]])  # Two detections
    safety_radius_pixels = 10

    safety_mask = create_safety_mask(frame_height, frame_width, boxes_centers, safety_radius_pixels)

    # Create multiple overlapping danger masks
    segment_danger_mask = np.roll(safety_mask, shift=9, axis=0)  # Shift slightly
    dem_nodata_danger_mask = np.roll(safety_mask, shift=5, axis=0)  # Shift slightly
    geofencing_danger_mask = np.roll(safety_mask, shift=5, axis=1)  # Shift slightly
    slope_danger_mask = np.roll(safety_mask, shift=9, axis=1)  # Shift slightly

    combined_danger_mask, combined_intersections, danger_types = create_dangerous_intersections_masks(
        frame_height,
        frame_width,
        boxes_centers,
        safety_radius_pixels,
        segment_danger_mask,
        dem_nodata_danger_mask,
        geofencing_danger_mask,
        slope_danger_mask
    )

    assert np.any(combined_intersections > 0)
    assert set(danger_types) == {
        "Vehicles Danger",
        "Missing DEM data Danger",
        "Out of Geofenced area Danger",
        "Steep slope Danger"}
    assert np.all((combined_danger_mask + combined_intersections) <= 1)  # Ensure binary mask
    assert np.all(0 <= (combined_danger_mask + combined_intersections))  # Ensure binary mask


def test_no_false_positives():
    """Ensure that safety areas outside danger masks do not produce false intersections."""
    frame_height, frame_width = 100, 100
    boxes_centers = np.array([[10, 10]])  # Detection far from any danger
    safety_radius_pixels = 10

    # Danger masks are in the opposite corner
    segment_danger_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
    segment_danger_mask[90:100, 90:100] = 1
    dem_nodata_danger_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
    geofencing_danger_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
    slope_danger_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)

    combined_danger_mask, combined_intersections, danger_types = create_dangerous_intersections_masks(
        frame_height,
        frame_width,
        boxes_centers,
        safety_radius_pixels,
        segment_danger_mask,
        dem_nodata_danger_mask,
        geofencing_danger_mask,
        slope_danger_mask
    )

    assert np.all(combined_intersections == 0)
    assert danger_types == []


def test_combined_mask_or_construction():
    """Ensure that the combined danger and intersection masks are correctly computed as the OR of the single masks."""
    frame_height, frame_width = 100, 100
    boxes_centers = np.array([[50, 50]])
    safety_radius_pixels = 10

    # Create a safety mask
    safety_mask = create_safety_mask(frame_height, frame_width, boxes_centers, safety_radius_pixels)

    # Define individual danger masks
    segment_danger_mask = np.random.randint(0, 2, (frame_height, frame_width), dtype=np.uint8)
    dem_nodata_danger_mask = np.random.randint(0, 2, (frame_height, frame_width), dtype=np.uint8)
    geofencing_danger_mask = np.random.randint(0, 2, (frame_height, frame_width), dtype=np.uint8)
    slope_danger_mask = np.random.randint(0, 2, (frame_height, frame_width), dtype=np.uint8)

    # Compute expected combined OR masks
    expected_combined_danger = merge_3d_mask(np.stack([
        segment_danger_mask,
        dem_nodata_danger_mask,
        geofencing_danger_mask,
        slope_danger_mask,
    ]))

    expected_combined_intersections = merge_3d_mask(np.stack([
        np.logical_and(safety_mask, segment_danger_mask),
        np.logical_and(safety_mask, dem_nodata_danger_mask),
        np.logical_and(safety_mask, geofencing_danger_mask),
        np.logical_and(safety_mask, slope_danger_mask),
    ]))

    # the returned danger mask is complementary to the intersection mask
    expected_combined_danger = expected_combined_danger - expected_combined_intersections

    # Run the function
    combined_danger_mask, combined_intersections, _ = create_dangerous_intersections_masks(
        frame_height,
        frame_width,
        boxes_centers,
        safety_radius_pixels,
        segment_danger_mask,
        dem_nodata_danger_mask,
        geofencing_danger_mask,
        slope_danger_mask,
    )

    # Check if the OR operation is correctly applied
    assert np.array_equal(combined_danger_mask, expected_combined_danger), "Combined danger mask is incorrect"
    assert np.array_equal(combined_intersections, expected_combined_intersections), "Combined intersection mask is incorrect"


def test_output_shape():
    """Ensure the function returns a 3D array with shape (1, H, W)"""
    elev_array = np.array([[[1, 2, 3], [4, 5, 6], [7, 8, 9]]])  # Shape (1,3,3)
    pixel_size = 1.0
    slope_threshold_deg = 10

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)

    assert mask.shape == elev_array.shape, "Output mask shape mismatch"


def test_zero_slope():
    """Test case with a constant elevation array (should produce all zeros)."""
    elev_array = np.ones((1, 5, 5)) * 100  # Flat terrain
    pixel_size = 1.0
    slope_threshold_deg = 5.0

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)

    assert np.all(mask == 0), "Mask should be all zeros for a flat terrain"


def test_basic_threshold_1():
    """Ensure function correctly classifies slopes above and below threshold."""
    elev_array = np.array([[
        [0, 0, 0, 0],  # Flat row
        [0, 0, 0, 0],  # Flat row
        [0, 0, 10, 20],  # Increasing elevation
        [0, 0, 20, 40]  # Even steeper
    ]])
    pixel_size = 1.0
    slope_threshold_deg = 10

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)

    expected_mask = np.array([[
        [0, 0, 0, 0],
        [0, 0, 1, 1],
        [0, 1, 1, 1],  # Middle row should have some slopes > 10 degrees
        [0, 1, 1, 1]
    ]])

    assert np.array_equal(mask, expected_mask), f"Mask does not match expected output, {expected_mask}"


def test_basic_threshold_2():
    """Ensure function correctly classifies slopes above and below threshold."""
    elev_array = np.array([[
        [0, 0, 0],  # Flat row
        [0, 10, 20],  # Increasing elevation
        [0, 20, 40]  # Even steeper
    ]])
    pixel_size = 1.0
    slope_threshold_deg = 10

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)

    expected_mask = np.array([[
        [0, 1, 1],
        [1, 1, 1],  # Middle row should have some slopes > 10 degrees
        [1, 1, 1]
    ]])

    assert np.array_equal(mask, expected_mask), f"Mask does not match expected output, {expected_mask}"


def test_steep_slope():
    """Test a very steep slope that should trigger a full mask of ones."""
    elev_array = np.array([[
        [0, 100, 200],  # Large elevation jumps
        [300, 400, 500],
        [600, 700, 800]
    ]])
    pixel_size = 1.0
    slope_threshold_deg = 1.0  # Very low threshold

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)

    assert np.all(mask == 1), f"All pixels should be marked as danger with such a low threshold"


def test_large_pixel_size():
    """Ensure the function correctly handles large pixel sizes (affects gradient calculation)."""
    elev_array = np.array([[
        [0, 5, 10],
        [5, 10, 15],
        [10, 15, 20]
    ]])
    pixel_size = 1000.0  # Larger pixel size should reduce slope values
    slope_threshold_deg = 5.0

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)

    assert np.all(mask == 0), "With large pixel size, some slopes should be below the threshold"


def test_single_high_point():
    """Test case where only one point is higher than all others."""
    elev_array = np.zeros((1, 5, 5))
    elev_array[0, 2, 2] = 100  # Single peak in the middle
    pixel_size = 1.0
    slope_threshold_deg = 20.0

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)

    assert np.sum(mask) > 0, "Mask should detect slope around the peak"


def test_non_square_array():
    """Ensure function works for non-square elevation arrays."""
    elev_array = np.array([[
        [0, 10, 20, 30],
        [10, 20, 30, 40]
    ]])  # Shape (1,2,4)
    pixel_size = 1.0
    slope_threshold_deg = 5.0

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)

    assert mask.shape == elev_array.shape, "Output shape should match input shape"


def test_extreme_threshold():
    """Ensure that a very high threshold results in all zeros."""
    elev_array = np.array([[
        [0, 10, 20],
        [10, 20, 30],
        [20, 30, 40]
    ]])
    pixel_size = 1.0
    slope_threshold_deg = 90.0  # Almost impossible threshold

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)

    assert np.all(mask == 0), "With a 90-degree threshold, no slopes should be detected"


def test_minimum_threshold():
    """Ensure that a zero-degree threshold results in all ones (except for a flat surface)."""
    elev_array = np.array([[
        [0, 5, 10],
        [5, 10, 15],
        [10, 15, 20]
    ]])
    pixel_size = 1.0
    slope_threshold_deg = 0.0  # Any slope is above zero

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)

    assert np.all(mask == 1), "All slopes should be marked with a zero-degree threshold"


def test_below_threshold():
    """Ensure that a being slightly below the threshold results in all zeros."""
    elev_array = np.array([[
        [0, 0, 0],
        [5, 5, 5],
        [5, 5, 5]
    ]])
    pixel_size = 10.0
    slope_threshold_deg = 15    # arctan(5/20) = 14 degrees

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)

    assert np.sum(mask == 1) == 0, "Single slope should be slightly below the threshold"


def test_above_threshold():
    """Ensure that a being slightly above the threshold results in some ones zeros."""
    elev_array = np.array([[
        [0, 0, 0],
        [5, 5, 5],
        [5, 5, 5]
    ]])
    pixel_size = 10.0
    slope_threshold_deg = 13    # arctan(5/20) = 14 degrees

    mask = compute_slope_mask_runtime(elev_array, pixel_size, slope_threshold_deg)
    assert np.sum(mask == 1) == 6, f"Single slope should be slightly above the threshold, got {mask}"
    # first row os 1 for padding with zeros 0-0-5 = delta=5 / 20
    # second row for matrix values 0-5-5 = delta=5 / 20
