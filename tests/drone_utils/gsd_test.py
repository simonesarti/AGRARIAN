import pytest
from app.shared.drone_utils.gsd import get_meters_per_pixel


# **Test valid calculations**
@pytest.mark.parametrize(
    "rel_altitude_m, \
    focal_length_mm, \
    sensor_width_mm, \
    sensor_height_mm, \
    sensor_width_pixels, \
    sensor_height_pixels, \
    image_width_pixels, \
    image_height_pixels, \
    expected",
    [
        # 4:3 sensor, 16:9 output
        (100.0, 10.0, 4.0, 3.0, 4000, 3000, 1920, 1080,
            10 * 1e-3 * min(4000/1920, 3000/1080)),

        # 4:3 sensor, 4:3 output
        (100.0, 10.0, 4.0, 3.0, 4000, 3000, 4000, 3000,
            10 * 1e-3 * 1),

        # 4:3 sensor, 1:1 output
        (100.0, 10.0, 4.0, 3.0, 4000, 3000, 4000, 4000,
            10 * 1e-3 * min(1.0, 3000/4000)),

        # 4:3 sensor, 9:16 output
        (100.0, 10.0, 4.0, 3.0, 4000, 3000, 1080, 1920,
            10 * 1e-3 * min(4000/1080, 3000/1920)),

        # 4:3 sensor, 3:4 output
        (100.0, 10.0, 4.0, 3.0, 4000, 3000, 3000, 4000,
            10 * 1e-3 * min(4000/3000, 3000/4000)),
    ]
)
def test_valid_meters_per_pixel(
        rel_altitude_m,
        focal_length_mm,
        sensor_width_mm,
        sensor_height_mm,
        sensor_width_pixels,
        sensor_height_pixels,
        image_width_pixels,
        image_height_pixels,
        expected
):

    result = get_meters_per_pixel(
        rel_altitude_m,
        focal_length_mm,
        sensor_width_mm,
        sensor_height_mm,
        sensor_width_pixels,
        sensor_height_pixels,
        image_width_pixels,
        image_height_pixels
    )

    assert pytest.approx(result, rel=1e-6) == expected  # Floating-point precision tolerance

