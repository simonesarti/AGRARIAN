import pytest
import numpy as np
from app.shared.drone_utils.localization import get_objects_coordinates
import folium

# Precomputed expected outputs (approximated to 6 decimals) for each angle.
# The 9 points (in the order given below) are:
#  1. Top-left:         [0, 0]
#  2. Top-right:        [1919, 0]
#  3. Bottom-right:     [1919, 1079]
#  4. Bottom-left:      [0, 1079]
#  5. Top-left quadrant center:  [479, 269]
#  6. Top-right quadrant center: [1439, 269]
#  7. Bottom-right quadrant center: [1439, 809]
#  8. Bottom-left quadrant center:  [479, 809]
#  9. Center:           [959, 539]

expected_results = {
    0: np.array([
        [-74.011411, 40.753348],
        [-73.988611, 40.753348],
        [-73.988611, 40.743663],
        [-74.011411, 40.743663],

        [-74.005711, 40.750932],
        [-73.994289, 40.750932],
        [-73.994289, 40.746086],
        [-74.005711, 40.746086],

        [-74.000012, 40.748509],
    ]),
    90: np.array([
        [-73.993584, 40.757126],
        [-73.993584, 40.739889],
        [-74.006402, 40.739889],
        [-74.006402, 40.757126],

        [-73.996781, 40.752819],
        [-73.996781, 40.744201],
        [-74.003195, 40.744201],
        [-74.003195, 40.752819],

        [-73.999988, 40.748509],
    ]),
    180: np.array([
        [-73.988589, 40.743652],
        [-74.011389, 40.743652],
        [-74.011389, 40.753337],
        [-73.988589, 40.753337],

        [-73.994287, 40.746068],
        [-74.005687, 40.746068],
        [-74.005687, 40.750914],
        [-73.994287, 40.750914],

        [-73.999988, 40.748491],
    ]),
    270: np.array([
        [-74.006416, 40.739874],
        [-74.006416, 40.757111],
        [-73.993596, 40.757111],
        [-73.993596, 40.739874],

        [-74.003218, 40.744181],
        [-74.003218, 40.752799],
        [-73.996801, 40.752799],
        [-73.996801, 40.744181],

        [-74.000012, 40.748491],
    ]),

}

# The pixel coordinates for our test points.
coords = np.array([
    [0, 0],  # Top-left
    [1919, 0],  # Top-right
    [1919, 1079],  # Bottom-right
    [0, 1079],  # Bottom-left
    [479, 269],  # Top-left quadrant center
    [1439, 269],  # Top-right quadrant center
    [1439, 809],  # Bottom-right quadrant center
    [479, 809],  # Bottom-left quadrant center
    [959, 539],  # Center
])

center_lon = -74.0
center_lat = 40.7485
frame_width_pixels = 1920
frame_height_pixels = 1080
meters_per_pixel = 1


@pytest.mark.parametrize(("angle", "expected"), [
    (0, expected_results[0]),
    (90, expected_results[90]),
    (180, expected_results[180]),
    (270, expected_results[270]),
])
def test_localization(angle, expected):
    # Call the function under test.
    result = get_objects_coordinates(coords, center_lat, center_lon,
                                     frame_width_pixels, frame_height_pixels,
                                     meters_per_pixel, angle)

    # map_obj = show_coordinates_on_map(result)
    # map_obj.save(f"{angle}_map.html")  # Save as an HTML file to view in a browser

    # Ensure we get a numpy array of shape (9,2)
    assert isinstance(result, np.ndarray), "Result must be a numpy array."
    assert result.shape == (9, 2), "Result must have shape (9,2)."

    # Check that the computed coordinates are within a tolerance of the expected values.
    np.testing.assert_allclose(result, expected, atol=1e-4, err_msg=f"Mismatch for angle {angle}°")


def show_coordinates_on_map(coords: np.ndarray, zoom_start: int = 15):
    """
    Display geographical coordinates on an interactive map using Folium.

    Args:
        coords (np.ndarray): A numpy array of shape (N,2), where each row is [longitude, latitude].
        zoom_start (int): Initial zoom level for the map (default is 15).

    Returns:
        folium.Map: A Folium map object with the plotted coordinates.
    """
    if not isinstance(coords, np.ndarray) or coords.shape[1] != 2:
        raise ValueError("Input must be a NumPy array of shape (N,2) with longitude and latitude.")

    # Compute the center of the map (mean of latitudes and longitudes)
    center_lat, center_lon = np.mean(coords[:, 1]), np.mean(coords[:, 0])

    # Create a Folium map centered at the average location
    m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom_start)

    # Add each coordinate as a marker
    for lon, lat in coords:
        folium.Marker([lat, lon], popup=f"({lon}, {lat})").add_to(m)

    return m