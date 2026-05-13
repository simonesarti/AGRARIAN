from argparse import ArgumentParser
from pathlib import Path
import rasterio
import shapely.vectorized
from rasterio.merge import merge
import numpy as np
import matplotlib.pyplot as plt


def tif_to_png(tif_path, png_path, colormap='viridis'):
    with rasterio.open(tif_path) as dataset:
        # Read the first band
        data = dataset.read(1)
        # Mask NoData values for better visualization
        nodata_value = dataset.nodata
        if nodata_value is not None:
            data = np.ma.masked_equal(data, nodata_value)

        # Plot the data and save as PNG
        plt.figure(figsize=(10, 10))
        plt.imshow(data, cmap=colormap)
        plt.colorbar(label='Elevation')  # Customize as needed
        plt.title('DEM Visualization')
        plt.xlabel("X")
        plt.ylabel("Y")
        plt.savefig(png_path, dpi=300, bbox_inches='tight', pad_inches=0.1)
        plt.close()

        print(f"PNG saved to: {png_path}")


def plot_2d_array(array, png_path, title="2D Array Plot", cmap="viridis", colorbar=True):
    """
    Plots a 2D NumPy array.

    Parameters:
    - array (numpy.ndarray): The 2D array to plot.
    - title (str): Title of the plot.
    - cmap (str): Colormap for the plot.
    - colorbar (bool): Whether to include a colorbar.
    """
    if array.ndim != 2:
        raise ValueError("The input array must be 2-dimensional.")

    plt.figure(figsize=(8, 6))
    plt.imshow(array, cmap=cmap, origin='upper')
    plt.title(title)
    plt.xlabel("X")
    plt.ylabel("Y")
    if colorbar:
        plt.colorbar(label="Value")
    plt.savefig(png_path, dpi=300, bbox_inches='tight', pad_inches=0.1)
    plt.close()

    print(f"PNG saved to: {png_path}")


def merge_dems():

    parser = ArgumentParser()
    parser.add_argument("--tifs_dir", type=str, required=True)
    parser.add_argument("--out_name", type=str, required=True)
    args = parser.parse_args()

    fill_value = 0

    tifs_dir_path = Path(args.tifs_dir)
    assert tifs_dir_path.is_dir(), f"Error: tifs_dir is not a valid path to a directory of tifs. Got {args.tifs_dir}"

    tifs_list = []
    sources = []
    for tif_file_path in tifs_dir_path.iterdir():
        if tif_file_path.suffix.lower() in [".tif", ".tiff"]:
            tifs_list.append(tif_file_path)
            sources.append(rasterio.open(tif_file_path, "r"))

    assert len(tifs_list) > 0, f"Error: no TIF files found inside the specified directory"

    nodata = None

    for tif_path, open_tif in zip(tifs_list, sources):
        nodata_value = open_tif.nodata
        read_tif = open_tif.read(1)
        size = read_tif.size
        nodata_count = np.sum(read_tif == nodata_value)

        print(f"TIF profile for {tif_path}:")
        print(open_tif.profile)
        print(open_tif.bounds)
        print(f"Size: {size}")
        print(f"NoData Count: {nodata_count}")
        print(f"NoData Perc: {nodata_count/size * 100}%")
        print(f"dtype: {read_tif.dtype}")
        print(f"max: {np.max(read_tif)}")
        print(f"min: {np.min(read_tif)}")
        print("\n")

        if nodata_value is not None:
            nodata = nodata_value

    if nodata is None:
        nodata = np.nan
    print(f"NoData set to {nodata}")

    out_dir = Path(tifs_dir_path, "merged")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path_tif = Path(out_dir, f"{args.out_name}.tif")
    out_path_mask = Path(out_dir, f"{args.out_name}_mask.tif")
    out_path_png = Path(out_dir, f"{args.out_name}.png")
    out_path_mask_png = Path(out_dir, f"{args.out_name}_mask.png")

    # Merge TIFS
    merged_data, merged_transform = merge(sources=sources, nodata=nodata)
    print(merged_data.shape)

    # Create 2D mask of nodata values, set 0 in the DEM at those positions
    mask = np.isnan(merged_data[0]).astype(np.uint8)
    print(np.max(mask))
    print(np.min(mask))

    # Replace nodata values in merged_data with 0
    merged_data[0][mask == 1] = fill_value

    # Convert mask to 3D array (C,H,W)
    mask = np.expand_dims(mask, axis=0)

    # Get metadata from the first TIF
    out_meta = sources[0].meta.copy()
    out_meta.update({
        "driver": "GTiff",
        'count': 1,
        "height": merged_data.shape[1],
        "width": merged_data.shape[2],
        "transform": merged_transform,
        "nodata": None
    })

    out_meta_mask = sources[0].meta.copy()
    out_meta_mask.update({
        "driver": "GTiff",
        'count': 1,
        "height": merged_data.shape[1],
        "width": merged_data.shape[2],
        "transform": merged_transform,
        "nodata": None,
        'dtype': 'uint8',
    })

    # Write the merged DEM to the output file
    with rasterio.open(out_path_tif, "w", **out_meta) as dest:
        dest.write(merged_data)

    # Write the merged DEM mask to the output file
    with rasterio.open(out_path_mask, "w", **out_meta_mask) as dest:
        dest.write(mask)

    print(f"Merged DEM saved to: {out_path_tif}")
    print(f"Merged DEM mask saved to: {out_path_mask}")

    for open_tif in sources:
        open_tif.close()

    tif_to_png(out_path_tif, out_path_png)
    tif_to_png(out_path_mask, out_path_mask_png)

    # CHECK

    with rasterio.open(out_path_tif, "r") as merged_tif:
        read_tif = merged_tif.read(1)
        size = read_tif.size
        nodata_count = np.sum(read_tif == nodata)

        print(f"TIF profile for {out_path_tif}:")
        print(merged_tif.profile)
        print(merged_tif.bounds)
        print(f"Size: {size}")
        print(f"NoData Count: {nodata_count}")
        print(f"NoData Perc: {nodata_count / size * 100}%")
        print(f"dtype: {read_tif.dtype}")
        print(f"max: {np.max(read_tif)}")
        print(f"min: {np.min(read_tif)}")
        print("\n")

    with rasterio.open(out_path_mask, "r") as merged_tif:
        read_tif = merged_tif.read(1)
        size = read_tif.size
        nodata_count = np.sum(read_tif == nodata)

        print(f"TIF profile for {out_path_mask}:")
        print(merged_tif.profile)
        print(merged_tif.bounds)
        print(f"Size: {size}")
        print(f"NoData Count: {nodata_count}")
        print(f"NoData Perc: {nodata_count / size * 100}%")
        print(f"dtype: {read_tif.dtype}")
        print(f"max: {np.max(read_tif)}")
        print(f"min: {np.min(read_tif)}")
        print(f"dtype: {read_tif.dtype}")
        print(f"max: {np.max(read_tif)}")
        print(f"min: {np.min(read_tif)}")
        print("\n")


if __name__ == "__main__":
    merge_dems()
