def get_meters_per_pixel(
        rel_altitude_m: float,
        focal_length_mm: float,
        sensor_width_mm: float,
        sensor_height_mm: float,
        sensor_width_pixels: int,
        sensor_height_pixels: int,
        image_width_pixels: int,
        image_height_pixels: int,
):
    """
    Converts pixels to meters using the pinhole camera model for a drone camera pointing at the ground.
    This function uses the full camera sensor resolution to compute the ground resolution.
    Afterward, it scales the ground resolution to match the output image resolution.

    Parameters:
    - rel_altitude_m: The altitude of the drone in meters.
    - focal_length_mm: The true focal length of the camera in millimeters.
    - sensor_width_mm: The width of the camera sensor in millimeters.
    - sensor_height_mm: The height of the camera sensor in millimeters.
    - sensor_width_pixels: The sensor width in pixels (original sensor resolution).
    - sensor_height_pixels: The sensor height in pixels (original sensor resolution).
    - image_width_pixels: The output image width in pixels (final image resolution).
    - image_height_pixels: The output image height in pixels (final image resolution).

    Returns:
    - ground_resolution: Ground resolution in meters per pixels.
    """

    # Calculate ground resolution (in meters/pixel) for both axes using the full sensor resolution
    #    meters         millimeters
    # -------------- *  ------------
    #  millimeters        pixels

    ground_resolution = (rel_altitude_m / focal_length_mm) * (sensor_width_mm / sensor_width_pixels)

    # sensor 4:3 is more square than frame 16:9
    # while the mapping from 5280 to 1920 still covers the whole sensor,
    # applying the same scaling factor to the height results in a picture longer than 1080 vertically (1440).
    # The excess pixels are cut off, but the pixels to meters relationship for height remains that of 1440
    # therefore the scaling factor is the same as for the width

    downsampling_factor_x = sensor_width_pixels / image_width_pixels
    downsampling_factor_y = sensor_height_pixels / image_height_pixels
    downsampling_factor = min(downsampling_factor_x, downsampling_factor_y)

    ground_resolution = ground_resolution * downsampling_factor

    return ground_resolution
