import multiprocessing as mp
import numpy as np
from multiprocessing import shared_memory
from queue import Empty


class FrameBuffer:
    """
    Pool of shared memory slots for zero-copy frame passing between a single producer
    and a single consumer in a multiprocessing pipeline hop.

    The producer acquires a free slot, writes a frame into it, and passes the slot index
    downstream via a lightweight metadata queue. The consumer reads the frame from that
    slot and releases it back to the pool when done.

    Usage (parent process):
        buf = FrameBuffer(frame_shape=(720, 1280, 3), n_slots=3)
        # pass buf to both producer and consumer processes

    Producer:
        slot = buf.acquire()
        if slot is not None:
            buf.write(slot, frame)
            meta_queue.put(FrameSlotMetadata(..., slot_index=slot))
        else:
            # no free slot — drop frame, consumer is too slow

    Consumer:
        meta = meta_queue.get()
        frame = buf.read(meta.slot_index)
        buf.release(meta.slot_index)

    Cleanup:
        buf.close()    # call in every process that uses this buffer
        buf.unlink()   # call once in the parent process after all children finish
    """

    def __init__(self, frame_shape: tuple[int, int, int], n_slots: int = 3, dtype=np.uint8):
        self.frame_shape = frame_shape
        self.dtype = dtype
        self.n_slots = n_slots
        self._nbytes = int(np.prod(frame_shape)) * np.dtype(dtype).itemsize

        self._shm = [
            shared_memory.SharedMemory(create=True, size=self._nbytes)
            for _ in range(n_slots)
        ]
        self._names = [s.name for s in self._shm]
        self._attached = True

        self._free: mp.Queue = mp.Queue(maxsize=n_slots)
        for i in range(n_slots):
            self._free.put(i)

    # ------------------------------------------------------------------ #
    # Producer interface
    # ------------------------------------------------------------------ #

    def acquire(self) -> int | None:
        """Non-blocking: return a free slot index, or None if the pool is exhausted."""
        try:
            return self._free.get_nowait()
        except Empty:
            return None

    def write(self, slot_idx: int, frame: np.ndarray) -> None:
        """Copy frame into the given slot."""
        dst = np.ndarray(self.frame_shape, dtype=self.dtype, buffer=self._shm[slot_idx].buf)
        np.copyto(dst, frame)

    # ------------------------------------------------------------------ #
    # Consumer interface
    # ------------------------------------------------------------------ #

    def read(self, slot_idx: int) -> np.ndarray:
        """Return a copy of the frame stored in the given slot."""
        src = np.ndarray(self.frame_shape, dtype=self.dtype, buffer=self._shm[slot_idx].buf)
        return src.copy()

    def release(self, slot_idx: int) -> None:
        """Return a slot to the free pool once the consumer is done with it."""
        self._free.put(slot_idx)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Detach from shared memory. Call in every process that uses this buffer on exit."""
        if self._attached:
            for s in self._shm:
                s.close()
            self._attached = False

    def unlink(self) -> None:
        """Destroy the shared memory blocks. Call once in the parent after all processes finish."""
        for name in self._names:
            try:
                shared_memory.SharedMemory(name=name, create=False).unlink()
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------------ #
    # Pickling support (required for mp.Process with 'spawn' start method)
    # ------------------------------------------------------------------ #

    def __getstate__(self) -> dict:
        return {
            'frame_shape': self.frame_shape,
            'dtype': self.dtype,
            'n_slots': self.n_slots,
            '_nbytes': self._nbytes,
            '_names': self._names,
            '_free': self._free,
        }

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._shm = [
            shared_memory.SharedMemory(create=False, name=name)
            for name in self._names
        ]
        self._attached = True
