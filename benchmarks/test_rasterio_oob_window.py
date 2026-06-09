"""
Test what rasterio does with a Window that extends outside the raster bounds:
  - Does Window() clip the offsets/size?
  - What does bounds() return — full requested area or clipped area?
  - What does dem_tif.read(window=...) do — error, silently clip, or pad?
"""
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.windows import bounds, Window

# --- Build a small 10x10 raster in memory ---
# Geographic extent: lon 0..10, lat 0..10  =>  1 deg/pixel
W, H = 10, 10
transform = from_bounds(west=0, south=0, east=10, north=10, width=W, height=H)
# pixel (row=0, col=0) -> NW corner (lon=0, lat=10)
# pixel (row=9, col=9) -> SE corner (lon=10, lat=0)

data = np.arange(1, W * H + 1, dtype=np.float32).reshape(1, H, W)  # shape (1,10,10)

with MemoryFile() as memfile:
    with memfile.open(
        driver="GTiff",
        count=1,
        dtype="float32",
        width=W,
        height=H,
        crs="EPSG:4326",
        transform=transform
    ) as dst:
        dst.write(data)

    with memfile.open() as src:
        print(f"Raster size: {src.width}w x {src.height}h")
        print(f"Raster transform: {src.transform}")
        print()

        # ---------------------------------------------------------------
        # Case 1: window fully inside (baseline)
        # ---------------------------------------------------------------
        w_inside = Window(col_off=2, row_off=2, width=4, height=4)
        print("=== Case 1: window fully inside ===")
        print(f"  Window:     col_off={w_inside.col_off}, row_off={w_inside.row_off}, "
              f"width={w_inside.width}, height={w_inside.height}")
        b = bounds(w_inside, src.transform)
        print(f"  bounds():   {b}")
        arr = src.read(window=w_inside)
        print(f"  read shape: {arr.shape}, values[0]: {arr[0]}")
        print()

        # ---------------------------------------------------------------
        # Case 2: window starts before col=0 (col_off negative)
        # ---------------------------------------------------------------
        w_neg_col = Window(col_off=-3, row_off=2, width=6, height=4)
        print("=== Case 2: window starts before col=0 (col_off=-3, width=6) ===")
        print(f"  Window obj: col_off={w_neg_col.col_off}, row_off={w_neg_col.row_off}, "
              f"width={w_neg_col.width}, height={w_neg_col.height}")
        b = bounds(w_neg_col, src.transform)
        print(f"  bounds():   {b}  <-- should extend west of lon=0")
        try:
            arr = src.read(window=w_neg_col)
            print(f"  read OK — shape={arr.shape}, values[0]: {arr[0]}")
        except Exception as e:
            print(f"  read RAISED: {type(e).__name__}: {e}")
        print()

        # ---------------------------------------------------------------
        # Case 3: window extends past col=width-1 (right edge overflow)
        # ---------------------------------------------------------------
        w_right = Window(col_off=7, row_off=2, width=6, height=4)  # col 7..12, raster ends at 9
        print("=== Case 3: window extends past right edge (col_off=7, width=6, raster width=10) ===")
        print(f"  Window obj: col_off={w_right.col_off}, row_off={w_right.row_off}, "
              f"width={w_right.width}, height={w_right.height}")
        b = bounds(w_right, src.transform)
        print(f"  bounds():   {b}  <-- should extend east of lon=10")
        try:
            arr = src.read(window=w_right)
            print(f"  read OK — shape={arr.shape}, values[0]: {arr[0]}")
        except Exception as e:
            print(f"  read RAISED: {type(e).__name__}: {e}")
        print()

        # ---------------------------------------------------------------
        # Case 4: intersection() to get the clipped window
        # ---------------------------------------------------------------
        raster_window = Window(col_off=0, row_off=0, width=src.width, height=src.height)
        print("=== Case 4: Window.intersection() with raster bounds ===")
        for label, w_oob in [("neg col", w_neg_col), ("right overflow", w_right)]:
            try:
                clipped = w_oob.intersection(raster_window)
                print(f"  {label}: intersection col_off={clipped.col_off}, row_off={clipped.row_off}, "
                      f"width={clipped.width}, height={clipped.height}")
                b_clipped = bounds(clipped, src.transform)
                print(f"    bounds of clipped: {b_clipped}  <-- this IS clamped to raster extent")
            except Exception as e:
                print(f"  {label}: intersection RAISED: {type(e).__name__}: {e}")
        print()

        # ---------------------------------------------------------------
        # Case 5: window entirely outside — what does read() do?
        # ---------------------------------------------------------------
        w_outside = Window(col_off=15, row_off=15, width=4, height=4)
        print("=== Case 5: window entirely outside ===")
        b = bounds(w_outside, src.transform)
        print(f"  bounds():   {b}  <-- should be well east/south of raster")
        try:
            arr = src.read(window=w_outside)
            print(f"  read OK — shape={arr.shape}")
        except Exception as e:
            print(f"  read RAISED: {type(e).__name__}: {e}")
