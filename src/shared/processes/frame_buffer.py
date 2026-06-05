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

    Consumer (copy, release immediately):
        meta = meta_queue.get()
        frame = buf.read(meta.slot_index)
        buf.release(meta.slot_index)

    Consumer (zero-copy view, release after last use):
        meta = meta_queue.get()
        view = buf.view(meta.slot_index)
        # ... all operations that read from view ...
        buf.release(meta.slot_index)

    Cleanup:
        buf.close()    # call in every process that uses this buffer
        buf.unlink()   # call once in the parent process after all children finish
    """

    def __init__(
        self,
        frame_shape: tuple[int, int, int],
        n_slots: int = 3,
        dtype=np.uint8,
    ):
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

    def view(self, slot_idx: int) -> np.ndarray:
        """Return a zero-copy view of the frame stored in the given slot.

        The caller must not call release() until all reads from the returned
        array (and any views derived from it) are complete.
        """
        return np.ndarray(self.frame_shape, dtype=self.dtype, buffer=self._shm[slot_idx].buf)

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


class MultiFrameBuffer:
    """
    Pool of shared memory slots backed by two separate SHM regions — a primary and a
    secondary — governed by a single slot pool.

    A single acquire()/release() call manages both regions through a shared slot index,
    avoiding the two-lock coordination problem of pairing two independent FrameBuffers.

    Intended use: pipeline hops where two related arrays (e.g. a BGR frame and a mask
    stack) are always produced and consumed together.

    Usage (parent process):
        buf = MultiFrameBuffer(
            primary_shape=(720, 1280, 3),    # HWC frame
            secondary_shape=(2, 720, 1280),  # CHW mask stack — [0]=danger, [1]=intersection
            n_slots=3,
        )

    Producer:
        slot = buf.acquire()
        if slot is not None:
            buf.write(slot, frame, masks)
            meta_queue.put(DangerSlotMetadata(..., slot_index=slot))
        else:
            # no free slot — drop frame, consumer too slow

    Consumer (zero-copy):
        meta = meta_queue.get()
        frame_view, mask_view = buf.view(meta.slot_index)
        danger_mask      = mask_view[0]   # contiguous (H, W)
        intersection_mask = mask_view[1]  # contiguous (H, W)
        # ... process ...
        buf.release(meta.slot_index)

    Cleanup:
        buf.close()    # call in every process that uses this buffer
        buf.unlink()   # call once in the parent process after all children finish
    """

    def __init__(
        self,
        primary_shape: tuple,
        secondary_shape: tuple,
        n_slots: int = 3,
        primary_dtype=np.uint8,
        secondary_dtype=np.uint8,
    ):
        self.primary_shape = primary_shape
        self.secondary_shape = secondary_shape
        self.primary_dtype = primary_dtype
        self.secondary_dtype = secondary_dtype
        self.n_slots = n_slots

        self._primary_nbytes = int(np.prod(primary_shape)) * np.dtype(primary_dtype).itemsize
        self._secondary_nbytes = int(np.prod(secondary_shape)) * np.dtype(secondary_dtype).itemsize

        self._primary_shm = [
            shared_memory.SharedMemory(create=True, size=self._primary_nbytes)
            for _ in range(n_slots)
        ]
        self._secondary_shm = [
            shared_memory.SharedMemory(create=True, size=self._secondary_nbytes)
            for _ in range(n_slots)
        ]

        self._primary_names = [s.name for s in self._primary_shm]
        self._secondary_names = [s.name for s in self._secondary_shm]
        self._attached = True

        self._free: mp.Queue = mp.Queue(maxsize=n_slots)
        for i in range(n_slots):
            self._free.put(i)

    # ------------------------------------------------------------------ #
    # Producer interface
    # ------------------------------------------------------------------ #

    def acquire(self) -> int | None:
        """Non-blocking: return a free slot index valid for both SHM regions, or None."""
        try:
            return self._free.get_nowait()
        except Empty:
            return None

    def write(self, slot_idx: int, primary: np.ndarray, secondary: np.ndarray) -> None:
        """Copy primary and secondary arrays into the given slot."""
        dst_p = np.ndarray(self.primary_shape, dtype=self.primary_dtype, buffer=self._primary_shm[slot_idx].buf)
        dst_s = np.ndarray(self.secondary_shape, dtype=self.secondary_dtype, buffer=self._secondary_shm[slot_idx].buf)
        np.copyto(dst_p, primary)
        np.copyto(dst_s, secondary)

    # ------------------------------------------------------------------ #
    # Consumer interface
    # ------------------------------------------------------------------ #
    
    def read(self, slot_idx: int) -> tuple[np.ndarray, np.ndarray]:
        """Return independent copies of both arrays stored in the given slot."""
        primary_view, secondary_view = self.view(slot_idx)
        return primary_view.copy(), secondary_view.copy()
    
    def view(self, slot_idx: int) -> tuple[np.ndarray, np.ndarray]:
        """Return zero-copy views of both SHM regions for the given slot.
        The caller must not call release() until all reads from both returned
        arrays (and any views derived from them) are complete.
        """
        primary_view = np.ndarray(self.primary_shape, dtype=self.primary_dtype, buffer=self._primary_shm[slot_idx].buf)
        secondary_view = np.ndarray(self.secondary_shape, dtype=self.secondary_dtype, buffer=self._secondary_shm[slot_idx].buf)
        return primary_view, secondary_view

    def release(self, slot_idx: int) -> None:
        """Return slot to the free pool once the consumer is done with both regions."""
        self._free.put(slot_idx)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Detach from shared memory. Call in every process that uses this buffer on exit."""
        if self._attached:
            for s in self._primary_shm:
                s.close()
            for s in self._secondary_shm:
                s.close()
            self._attached = False

    def unlink(self) -> None:
        """Destroy the shared memory blocks. Call once in the parent after all processes finish."""
        for names in (self._primary_names, self._secondary_names):
            for name in names:
                try:
                    shared_memory.SharedMemory(name=name, create=False).unlink()
                except FileNotFoundError:
                    pass

    # ------------------------------------------------------------------ #
    # Pickling support (required for mp.Process with 'spawn' start method)
    # ------------------------------------------------------------------ #

    def __getstate__(self) -> dict:
        return {
            'primary_shape': self.primary_shape,
            'secondary_shape': self.secondary_shape,
            'primary_dtype': self.primary_dtype,
            'secondary_dtype': self.secondary_dtype,
            'n_slots': self.n_slots,
            '_primary_nbytes': self._primary_nbytes,
            '_secondary_nbytes': self._secondary_nbytes,
            '_primary_names': self._primary_names,
            '_secondary_names': self._secondary_names,
            '_free': self._free,
        }

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._primary_shm = [
            shared_memory.SharedMemory(create=False, name=name)
            for name in self._primary_names
        ]
        self._secondary_shm = [
            shared_memory.SharedMemory(create=False, name=name)
            for name in self._secondary_names
        ]
        self._attached = True
