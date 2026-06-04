"""
benchmark_transport.py

Compares three strategies for passing a stacked (H, W, C) frame from a
producer process to a consumer process, measuring both the producer write
cost and the consumer cost to have all channels extracted and ready.

Strategies:
  queue_pickle   — numpy array pickled onto a multiprocessing.Queue
  shm_copy       — SharedMemory slot + metadata queue; consumer calls .copy()
                   then releases (current FrameBuffer behaviour)
  shm_view       — SharedMemory slot + metadata queue; consumer takes a zero-
                   copy view, extracts channels, then releases

For each strategy the consumer extracts the same data:
  bgr    = contiguous (H, W, 3)
  mask_0 = contiguous (H, W)   [channel 3]
  mask_1 = contiguous (H, W)   [channel 4]

Run:
    python benchmark_transport.py [--shape HxWxC] [--frames N] [--slots K]
"""

import argparse
import multiprocessing as mp
import time
from multiprocessing import shared_memory
from statistics import mean, median, stdev

import numpy as np

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ms(t): return t * 1000


def stats(values):
    arr = sorted(values)
    n   = len(arr)
    return {
        "mean":   mean(arr),
        "median": median(arr),
        "p95":    arr[int(n * 0.95)],
        "min":    arr[0],
        "max":    arr[-1],
    }


def print_stats(label, put_times, get_times, e2e_times):
    print(f"\n  {label}")
    for name, values in [("PUT (producer)", put_times),
                          ("GET (consumer)", get_times),
                          ("END-TO-END",     e2e_times)]:
        s = stats([_ms(v) for v in values])
        print(f"    {name:20s}  mean={s['mean']:6.2f}  median={s['median']:6.2f}"
              f"  p95={s['p95']:6.2f}  min={s['min']:6.2f}  max={s['max']:6.2f}  ms")
    fps = 1.0 / mean(e2e_times)
    print(f"    {'throughput':20s}  {fps:.1f} fps  (1/mean e2e)")


# ---------------------------------------------------------------------------
# strategy 1 — queue pickle
# ---------------------------------------------------------------------------

def _producer_queue(q, shape, n_frames, n_warmup, ready_ev, result_q):
    frame = np.random.randint(0, 256, shape, dtype=np.uint8)
    ready_ev.wait()

    put_times = []
    send_ts   = []

    for i in range(n_warmup + n_frames):
        frame[0, 0, 0] = i % 256
        t0 = time.perf_counter()
        q.put((time.perf_counter(), frame))   # embed send timestamp
        t1 = time.perf_counter()
        if i >= n_warmup:
            put_times.append(t1 - t0)

    q.put(None)
    result_q.put(put_times)


def _consumer_queue(q, n_frames, n_warmup, ready_ev, result_q):
    ready_ev.set()
    get_times = []
    e2e_times = []
    count = 0

    while True:
        item = q.get()
        if item is None:
            break
        send_ts, frame = item
        t0 = time.perf_counter()
        bgr    = np.ascontiguousarray(frame[:, :, :3])
        # mask_0 = np.ascontiguousarray(frame[:, :, 3])
        # mask_1 = np.ascontiguousarray(frame[:, :, 4])
        # bgr    = frame[:, :, :3]
        mask_0 = frame[:, :, 3]
        mask_1 = frame[:, :, 4]
        t1 = time.perf_counter()
        count += 1
        if count > n_warmup:
            get_times.append(t1 - t0)
            e2e_times.append(t1 - send_ts)

    result_q.put((get_times, e2e_times))


def run_queue(shape, n_frames, n_warmup):
    q        = mp.Queue(maxsize=8)
    result_q = mp.Queue()
    ready_ev = mp.Event()

    cons = mp.Process(target=_consumer_queue, args=(q, n_frames, n_warmup, ready_ev, result_q))
    prod = mp.Process(target=_producer_queue, args=(q, shape, n_frames, n_warmup, ready_ev, result_q))

    cons.start(); prod.start()
    prod.join();  cons.join()

    put_times           = result_q.get()
    get_times, e2e_times = result_q.get()
    return put_times, get_times, e2e_times


# ---------------------------------------------------------------------------
# strategy 2 — shared memory + copy   (current FrameBuffer behaviour)
# ---------------------------------------------------------------------------

def _producer_shm(shm_names, shape, meta_q, free_q, n_frames, n_warmup, ready_ev, result_q):
    dtype = np.uint8
    shms  = [shared_memory.SharedMemory(create=False, name=n) for n in shm_names]
    frame = np.random.randint(0, 256, shape, dtype=dtype)
    ready_ev.wait()

    put_times = []
    for i in range(n_warmup + n_frames):
        slot = free_q.get()
        frame[0, 0, 0] = i % 256
        t0  = time.perf_counter()
        dst = np.ndarray(shape, dtype=dtype, buffer=shms[slot].buf)
        np.copyto(dst, frame)
        send_ts = time.perf_counter()
        meta_q.put((slot, send_ts))
        t1 = time.perf_counter()
        if i >= n_warmup:
            put_times.append(t1 - t0)

    meta_q.put(None)
    result_q.put(put_times)
    for s in shms: s.close()


def _consumer_shm_copy(shm_names, shape, meta_q, free_q, n_warmup, result_q):
    dtype = np.uint8
    shms  = [shared_memory.SharedMemory(create=False, name=n) for n in shm_names]
    get_times, e2e_times = [], []
    count = 0

    while True:
        item = meta_q.get()
        if item is None:
            break
        slot, send_ts = item
        t0      = time.perf_counter()
        src     = np.ndarray(shape, dtype=dtype, buffer=shms[slot].buf)
        stacked = src.copy()                        # full copy before release
        free_q.put(slot)
        bgr    = np.ascontiguousarray(stacked[:, :, :3])
        # mask_0 = np.ascontiguousarray(stacked[:, :, 3])
        # mask_1 = np.ascontiguousarray(stacked[:, :, 4])
        # bgr    = stacked[:, :, :3]
        mask_0 = stacked[:, :, 3]
        mask_1 = stacked[:, :, 4]
        t1 = time.perf_counter()
        count += 1
        if count > n_warmup:
            get_times.append(t1 - t0)
            e2e_times.append(t1 - send_ts)

    result_q.put((get_times, e2e_times))
    for s in shms: s.close()


def _consumer_shm_view(shm_names, shape, meta_q, free_q, n_warmup, result_q):
    dtype = np.uint8
    shms  = [shared_memory.SharedMemory(create=False, name=n) for n in shm_names]
    get_times, e2e_times = [], []
    count = 0

    while True:
        item = meta_q.get()
        if item is None:
            break
        slot, send_ts = item
        t0      = time.perf_counter()
        stacked = np.ndarray(shape, dtype=dtype, buffer=shms[slot].buf)  # zero-copy view
        bgr    = np.ascontiguousarray(stacked[:, :, :3])
        # mask_0 = np.ascontiguousarray(stacked[:, :, 3])
        # mask_1 = np.ascontiguousarray(stacked[:, :, 4])
        # bgr    = stacked[:, :, :3]
        mask_0 = stacked[:, :, 3]
        mask_1 = stacked[:, :, 4]
        free_q.put(slot)                                                  # release after extract
        t1 = time.perf_counter()
        count += 1
        if count > n_warmup:
            get_times.append(t1 - t0)
            e2e_times.append(t1 - send_ts)

    result_q.put((get_times, e2e_times))
    for s in shms: s.close()


def _run_shm(consumer_fn, shape, n_frames, n_warmup, n_slots):
    dtype  = np.uint8
    nbytes = int(np.prod(shape)) * np.dtype(dtype).itemsize
    shms   = [shared_memory.SharedMemory(create=True, size=nbytes) for _ in range(n_slots)]
    names  = [s.name for s in shms]

    meta_q   = mp.Queue(maxsize=8)
    free_q   = mp.Queue(maxsize=n_slots)
    result_q = mp.Queue()
    ready_ev = mp.Event()
    for i in range(n_slots):
        free_q.put(i)

    cons = mp.Process(target=consumer_fn,
                      args=(names, shape, meta_q, free_q, n_warmup, result_q))
    prod = mp.Process(target=_producer_shm,
                      args=(names, shape, meta_q, free_q, n_frames, n_warmup, ready_ev, result_q))

    cons.start(); ready_ev.set(); prod.start()
    prod.join();  cons.join()

    put_times            = result_q.get()
    get_times, e2e_times = result_q.get()

    for s in shms: s.close(); s.unlink()
    return put_times, get_times, e2e_times


def run_shm_copy(shape, n_frames, n_warmup, n_slots):
    return _run_shm(_consumer_shm_copy, shape, n_frames, n_warmup, n_slots)


def run_shm_view(shape, n_frames, n_warmup, n_slots):
    return _run_shm(_consumer_shm_view, shape, n_frames, n_warmup, n_slots)


# ---------------------------------------------------------------------------
# strategy 4 — shared memory + packed sequential layout
#   slot layout: [H×W×3 BGR | H×W mask_0 | H×W mask_1 | … ]
#   each component is a contiguous block → plain reshape, no ascontiguousarray
# ---------------------------------------------------------------------------

def _producer_shm_packed(shm_names, shape, meta_q, free_q, n_frames, n_warmup, ready_ev, result_q):
    dtype      = np.uint8
    H, W, C    = shape
    bgr_bytes  = H * W * 3
    mask_bytes = H * W
    shms  = [shared_memory.SharedMemory(create=False, name=n) for n in shm_names]
    frame = np.random.randint(0, 256, shape, dtype=dtype)
    ready_ev.wait()

    put_times = []
    for i in range(n_warmup + n_frames):
        slot = free_q.get()
        frame[0, 0, 0] = i % 256
        t0   = time.perf_counter()
        flat = np.ndarray((H * W * C,), dtype=dtype, buffer=shms[slot].buf)
        np.copyto(flat[:bgr_bytes].reshape(H, W, 3), frame[:, :, :3])
        for ch in range(3, C):
            off = bgr_bytes + (ch - 3) * mask_bytes
            np.copyto(flat[off:off + mask_bytes].reshape(H, W), frame[:, :, ch])
        send_ts = time.perf_counter()
        meta_q.put((slot, send_ts))
        t1 = time.perf_counter()
        if i >= n_warmup:
            put_times.append(t1 - t0)

    meta_q.put(None)
    result_q.put(put_times)
    for s in shms: s.close()


def _consumer_shm_packed_copy(shm_names, shape, meta_q, free_q, n_warmup, result_q):
    dtype      = np.uint8
    H, W, C    = shape
    bgr_bytes  = H * W * 3
    mask_bytes = H * W
    shms  = [shared_memory.SharedMemory(create=False, name=n) for n in shm_names]
    get_times, e2e_times = [], []
    count = 0

    while True:
        item = meta_q.get()
        if item is None:
            break
        slot, send_ts = item
        t0   = time.perf_counter()
        flat = np.ndarray((H * W * C,), dtype=dtype, buffer=shms[slot].buf)
        bgr    = flat[:bgr_bytes                   ].copy().reshape(H, W, 3)
        mask_0 = flat[bgr_bytes:bgr_bytes +   mask_bytes].copy().reshape(H, W)
        mask_1 = flat[bgr_bytes + mask_bytes:bgr_bytes + 2 * mask_bytes].copy().reshape(H, W)
        free_q.put(slot)
        t1 = time.perf_counter()
        count += 1
        if count > n_warmup:
            get_times.append(t1 - t0)
            e2e_times.append(t1 - send_ts)

    result_q.put((get_times, e2e_times))
    for s in shms: s.close()


def _consumer_shm_packed_view(shm_names, shape, meta_q, free_q, n_warmup, result_q):
    dtype      = np.uint8
    H, W, C    = shape
    bgr_bytes  = H * W * 3
    mask_bytes = H * W
    shms  = [shared_memory.SharedMemory(create=False, name=n) for n in shm_names]
    get_times, e2e_times = [], []
    count = 0

    while True:
        item = meta_q.get()
        if item is None:
            break
        slot, send_ts = item
        t0   = time.perf_counter()
        flat   = np.ndarray((H * W * C,), dtype=dtype, buffer=shms[slot].buf)
        bgr    = flat[:bgr_bytes                        ].reshape(H, W, 3)
        mask_0 = flat[bgr_bytes:bgr_bytes +   mask_bytes].reshape(H, W)
        mask_1 = flat[bgr_bytes + mask_bytes:bgr_bytes + 2 * mask_bytes].reshape(H, W)
        free_q.put(slot)
        t1 = time.perf_counter()
        count += 1
        if count > n_warmup:
            get_times.append(t1 - t0)
            e2e_times.append(t1 - send_ts)

    result_q.put((get_times, e2e_times))
    for s in shms: s.close()


def _run_shm_packed(consumer_fn, shape, n_frames, n_warmup, n_slots):
    dtype  = np.uint8
    H, W, C = shape
    nbytes = H * W * C * np.dtype(dtype).itemsize
    shms   = [shared_memory.SharedMemory(create=True, size=nbytes) for _ in range(n_slots)]
    names  = [s.name for s in shms]

    meta_q   = mp.Queue(maxsize=8)
    free_q   = mp.Queue(maxsize=n_slots)
    result_q = mp.Queue()
    ready_ev = mp.Event()
    for i in range(n_slots):
        free_q.put(i)

    cons = mp.Process(target=consumer_fn,
                      args=(names, shape, meta_q, free_q, n_warmup, result_q))
    prod = mp.Process(target=_producer_shm_packed,
                      args=(names, shape, meta_q, free_q, n_frames, n_warmup, ready_ev, result_q))

    cons.start(); ready_ev.set(); prod.start()
    prod.join();  cons.join()

    put_times            = result_q.get()
    get_times, e2e_times = result_q.get()

    for s in shms: s.close(); s.unlink()
    return put_times, get_times, e2e_times


def run_shm_packed_copy(shape, n_frames, n_warmup, n_slots):
    return _run_shm_packed(_consumer_shm_packed_copy, shape, n_frames, n_warmup, n_slots)


def run_shm_packed_view(shape, n_frames, n_warmup, n_slots):
    return _run_shm_packed(_consumer_shm_packed_view, shape, n_frames, n_warmup, n_slots)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shape",  default="720x1280x8",
                        help="HxWxC of the stacked frame (default: 720x1280x8)")
    parser.add_argument("--frames", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--slots",  type=int, default=4,
                        help="Number of shared memory slots (default: 4)")
    args = parser.parse_args()

    H, W, C = map(int, args.shape.split("x"))
    shape    = (H, W, C)
    nbytes   = H * W * C / 1024 / 1024

    print(f"\nFrame: {W}×{H}×{C}  ({nbytes:.2f} MB)  |  "
          f"{args.frames} frames  |  {args.warmup} warmup  |  {args.slots} shm slots")
    print("=" * 72)

    print("\nRunning queue_pickle ...")
    put_q, get_q, e2e_q = run_queue(shape, args.frames, args.warmup)
    print_stats("queue_pickle", put_q, get_q, e2e_q)

    print("\nRunning shm_copy (current) ...")
    put_c, get_c, e2e_c = run_shm_copy(shape, args.frames, args.warmup, args.slots)
    print_stats("shm_copy     (current)", put_c, get_c, e2e_c)

    print("\nRunning shm_view (zero-copy) ...")
    put_v, get_v, e2e_v = run_shm_view(shape, args.frames, args.warmup, args.slots)
    print_stats("shm_view     (zero-copy)", put_v, get_v, e2e_v)

    print("\nRunning shm_packed_copy (packed sequential, copy) ...")
    put_pc, get_pc, e2e_pc = run_shm_packed_copy(shape, args.frames, args.warmup, args.slots)
    print_stats("shm_packed_copy (packed+copy)", put_pc, get_pc, e2e_pc)

    print("\nRunning shm_packed_view (packed sequential, zero-copy) ...")
    put_pv, get_pv, e2e_pv = run_shm_packed_view(shape, args.frames, args.warmup, args.slots)
    print_stats("shm_packed_view (packed+view)", put_pv, get_pv, e2e_pv)

    print("\n" + "=" * 72)
    print("Summary — mean end-to-end latency:")
    base = mean(_ms(x) for x in e2e_q)
    for label, e2e in [("queue_pickle      ", e2e_q),
                        ("shm_copy          ", e2e_c),
                        ("shm_view          ", e2e_v),
                        ("shm_packed_copy   ", e2e_pc),
                        ("shm_packed_view   ", e2e_pv)]:
        ms = mean(_ms(x) for x in e2e)
        print(f"  {label}  {ms:6.2f} ms  ({ms/base:.2f}× vs queue)")


if __name__ == "__main__":
    mp.set_start_method("spawn")
    main()
