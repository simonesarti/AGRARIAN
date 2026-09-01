#!/usr/bin/env python3
"""
generate_dem.py

Generate a synthetic Digital Elevation Model (DEM) covering a lat/lon
bounding box, at 1 meter/pixel resolution, made of a regular grid of
Gaussian "hills". Elevation (and therefore slope) rises smoothly from 0
at the valley floor between hills up to a configurable peak slope
(45 degrees by default) at a fixed hill spacing (50 m by default), then
back down to 0 -- exactly the "grid of small hills" pattern:

    .  .  .  .  .  .  .  .  .
    .  .  ^  .  .  ^  .  .  .
    .  .  .  .  .  .  .  .  .
    .  .  ^  .  .  ^  .  .  .
    .  .  .  .  .  .  .  .  .

Output: a GeoTIFF DEM in EPSG:4326 (lon/lat degrees), which is what the
danger-detection geo worker expects -- it indexes the DEM directly with the
drone's lon/lat and never reprojects. The degree step differs between the two
axes (a degree of longitude is cos(latitude) shorter than a degree of latitude)
so that one pixel still covers `resolution` meters of ground in BOTH directions.

No companion nodata mask is produced: the DEM has no nodata pixels, and the geo
worker treats a missing mask as "all DEM data valid".

Usage:
    python generate_dem.py --bbox 12.4900 41.8900 12.5000 41.9000 \
        --spacing 50 --peak-slope 45 --output rome_hills.tif

Requirements:
    pip install numpy rasterio pyproj
"""

import argparse
import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from pyproj import Geod


# WGS84 ellipsoid, the same one geopy's geodesic() uses in the geo worker when it
# measures the DEM window back into meters.
_GEOD = Geod(ellps="WGS84")


def meters_per_degree(lon: float, lat: float, delta_deg: float = 1e-3):
    """
    Return (meters per degree of longitude, meters per degree of latitude) at a point.

    Measured over a small delta centred on the point and scaled up, rather than
    over a full degree, so the result is the local rate rather than an average
    across a whole degree of the ellipsoid.
    """
    _, _, dist_lon = _GEOD.inv(lon - delta_deg / 2, lat, lon + delta_deg / 2, lat)
    _, _, dist_lat = _GEOD.inv(lon, lat - delta_deg / 2, lon, lat + delta_deg / 2)
    return dist_lon / delta_deg, dist_lat / delta_deg


def generate_dem(
    bbox,                    # (min_lon, min_lat, max_lon, max_lat)
    resolution=1.0,          # meters/pixel
    spacing=50.0,            # meters between hill peaks
    peak_slope_deg=45.0,     # target max slope, in degrees
    sigma_ratio=0.22,        # hill width as a fraction of spacing
    base_elevation=0.0,      # meters, flat offset added everywhere
    jitter=0.0,              # 0..1, randomly perturb hill centers (0 = perfect grid)
    seed=0,
):
    """
    Build a synthetic hilly DEM over `bbox` and return (dem, transform, crs).

    Hills sit on a regular grid `spacing` meters apart in both directions.
    Each hill is a 2D Gaussian bump. Because the bumps are narrow relative
    to their spacing, elevation at any pixel is (to excellent approximation)
    governed only by the nearest hill center, so we compute it directly
    with periodic (wrapped) distances instead of summing every hill's
    contribution -- this keeps things fast even for large areas.

    The terrain is laid out on a metric grid (so the hills are round on the
    ground and exactly `spacing` meters apart), and that grid is then written
    out with a lon/lat transform whose per-axis degree steps correspond to
    `resolution` meters at the center of the bbox. Consumer-drone flights cover
    at most a few km, so treating the local meters-per-degree as constant across
    the raster is accurate to well under one pixel.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2

    # Degree step per axis that corresponds to `resolution` meters on the ground.
    m_per_deg_lon, m_per_deg_lat = meters_per_degree(center_lon, center_lat)
    res_deg_lon = resolution / m_per_deg_lon
    res_deg_lat = resolution / m_per_deg_lat

    n_cols = max(1, int(round((max_lon - min_lon) / res_deg_lon)))
    n_rows = max(1, int(round((max_lat - min_lat) / res_deg_lat)))

    # Pixel-center coordinates, in local meters from the north-west corner of the
    # bbox, row 0 = north edge. Only distances matter for the hill pattern, so the
    # origin is arbitrary and taken as (0, 0).
    px_x = (np.arange(n_cols) + 0.5) * resolution
    px_y = (np.arange(n_rows) + 0.5) * resolution
    X, Y = np.meshgrid(px_x, px_y)  # shape (n_rows, n_cols)

    # Optional jitter so the hill grid feels less mechanical.
    rng = np.random.default_rng(seed)
    if jitter > 0:
        jx = (rng.random(X.shape) * 2 - 1) * jitter * spacing / 2
        jy = (rng.random(X.shape) * 2 - 1) * jitter * spacing / 2
    else:
        jx = jy = 0.0

    # Distance from each pixel to the nearest hill center on the grid,
    # using a wrapped (periodic) distance in each axis.
    def periodic_dist(coord):
        d = np.mod(coord, spacing)
        return np.minimum(d, spacing - d)

    dx = periodic_dist(X + jx)
    dy = periodic_dist(Y + jy)
    r = np.sqrt(dx**2 + dy**2)

    # Gaussian bump: h(r) = A * exp(-r^2 / (2*sigma^2))
    # Its slope magnitude peaks at r = sigma, where |dh/dr| = (A/sigma)*exp(-0.5).
    # Solve for A so that peak slope == tan(peak_slope_deg).
    sigma = spacing * sigma_ratio
    target_slope = np.tan(np.radians(peak_slope_deg))
    amplitude = target_slope * sigma / np.exp(-0.5)

    dem = base_elevation + amplitude * np.exp(-(r**2) / (2 * sigma**2))
    dem = dem.astype(np.float32)

    # Affine mapping (col, row) -> (lon, lat): north-up, no rotation, and a
    # different step per axis so both are `resolution` meters on the ground.
    transform = Affine(res_deg_lon, 0.0, min_lon,
                       0.0, -res_deg_lat, max_lat)
    return dem, transform, CRS.from_epsg(4326)


def write_geotiff(dem, transform, crs, output_path):
    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=dem.shape[0],
        width=dem.shape[1],
        count=1,
        dtype=dem.dtype,
        crs=crs,
        transform=transform,
        nodata=None,
        compress="deflate",
    ) as dst:
        dst.write(dem, 1)


def report_slope_stats(dem, resolution):
    gy, gx = np.gradient(dem, resolution)
    slope_deg = np.degrees(np.arctan(np.sqrt(gx**2 + gy**2)))
    print(f"DEM shape: {dem.shape[0]} rows x {dem.shape[1]} cols "
          f"({dem.shape[0]*dem.shape[1]:,} pixels)")
    print(f"Elevation range: {dem.min():.2f} m to {dem.max():.2f} m")
    print(f"Slope range: {slope_deg.min():.1f} deg to {slope_deg.max():.1f} deg "
          f"(mean {slope_deg.mean():.1f} deg)")


def main():
    parser = argparse.ArgumentParser(
        description="Generate a synthetic grid-of-hills DEM for a lat/lon bbox."
    )
    parser.add_argument(
        "--bbox", type=float, nargs=4, required=True,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help="Bounding box in decimal degrees, e.g. --bbox 12.49 41.89 12.50 41.90",
    )
    parser.add_argument("--resolution", type=float, default=1.0, help="meters/pixel (default 1.0)")
    parser.add_argument("--spacing", type=float, default=50.0, help="meters between hill peaks (default 50)")
    parser.add_argument("--peak-slope", type=float, default=45.0, help="target peak slope in degrees (default 45)")
    parser.add_argument("--sigma-ratio", type=float, default=0.22, help="hill width as fraction of spacing (default 0.22)")
    parser.add_argument("--base-elevation", type=float, default=0.0, help="flat elevation offset in meters (default 0)")
    parser.add_argument("--jitter", type=float, default=0.0, help="0-1, randomize hill positions (default 0 = regular grid)")
    parser.add_argument("--seed", type=int, default=0, help="random seed for jitter")
    parser.add_argument("--output", type=str, default="dem.tif", help="output GeoTIFF path")
    args = parser.parse_args()

    dem, transform, crs = generate_dem(
        bbox=tuple(args.bbox),
        resolution=args.resolution,
        spacing=args.spacing,
        peak_slope_deg=args.peak_slope,
        sigma_ratio=args.sigma_ratio,
        base_elevation=args.base_elevation,
        jitter=args.jitter,
        seed=args.seed,
    )
    write_geotiff(dem, transform, crs, args.output)
    report_slope_stats(dem, args.resolution)
    print(f"CRS: {crs.to_string()}")
    print(f"Pixel size: {transform.a:.3e} deg lon x {abs(transform.e):.3e} deg lat "
          f"(= {args.resolution} m x {args.resolution} m on the ground)")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
