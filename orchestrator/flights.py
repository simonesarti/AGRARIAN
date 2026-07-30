"""
Flight lifecycle: stream goes live → container exists; stream stops → it does not.

This is the whole point of the orchestrator and the reason it was written against the
FlightRuntime interface rather than against a cluster — none of what follows is
platform-specific.

Two things here are less obvious than they look.

**A dropped stream is usually not a finished flight.** MediaMTX fires the offline hook
the instant a publisher disconnects, including for a radio glitch the drone recovers
from seconds later. Tearing down immediately would mean a cold GPU start — model
weights reloaded from disk — for a blip, so teardown is deferred by a grace period and
cancelled if the same key comes back.

**Both hooks can fire more than once**, and can interleave. Every operation on one
stream key is therefore serialised behind its own lock, and both entry points are
idempotent: a second online for a running flight is a no-op, a second offline is
absorbed by the pending teardown.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("orchestrator.flights")


@dataclass
class Flight:
    flight_id: int
    public_uuid: str
    handle: str
    publisher_token: str
    # Set while teardown is waiting out the reconnect grace period. Its presence is
    # what makes a reconnect distinguishable from a fresh takeoff.
    teardown: Optional[asyncio.Task] = field(default=None, repr=False)


def build_flight_env(opened: dict, base_env: Dict[str, str]) -> Dict[str, str]:
    """
    The contract between the orchestrator and the app container.

    Everything identifying the flight is injected here and nowhere else. In particular
    the app receives a token scoped to this one flight instead of the operator's email
    and password, which is what gets end-user credentials out of the GPU tier.

    base_env carries the deployment-wide settings the operator configured on the
    orchestrator (service URLs, media host, model selection). Per-flight values win,
    so a stray VIDEO_OUT_STREAM_STREAM_KEY in the environment cannot redirect a
    tenant's annotated video somewhere else.
    """
    return {
        **base_env,
        "FLIGHT_ID": str(opened["flight_id"]),
        "PUBLISHER_TOKEN": opened["publisher_token"],
        # Paths, not full URLs: the app composes protocol/host/port itself, and the
        # naming scheme is db-writer's to decide.
        "VIDEO_STREAM_READER_STREAM_KEY": opened["ingest_path"],
        "VIDEO_OUT_STREAM_STREAM_KEY": opened["output_path"],
    }


class FlightOrchestrator:
    def __init__(self, runtime, directory, base_env: Dict[str, str], grace_s: float):
        self._runtime = runtime
        self._directory = directory      # db-writer client
        self._base_env = base_env
        self._grace_s = grace_s

        self._flights: Dict[str, Flight] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def active(self) -> int:
        return len(self._flights)

    def snapshot(self) -> list:
        return [
            {"flight_id": f.flight_id, "public_uuid": f.public_uuid,
             "tearing_down": f.teardown is not None}
            for f in self._flights.values()
        ]

    # ── Locking ───────────────────────────────────────────────────────────────

    def _lock_for(self, stream_key: str) -> asyncio.Lock:
        # Per key, not global: two tenants taking off at the same moment must not
        # queue behind each other's container start, which takes seconds.
        lock = self._locks.get(stream_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[stream_key] = lock
        return lock

    # ── Events ────────────────────────────────────────────────────────────────

    async def stream_online(self, stream_key: str) -> Optional[int]:
        """A publisher went live. Returns the flight_id, or None if the key is dead."""
        async with self._lock_for(stream_key):
            existing = self._flights.get(stream_key)

            if existing is not None:
                if existing.teardown is not None:
                    # The drone came back inside the grace period. Keep the container
                    # and the flight — this is the case the grace period exists for.
                    existing.teardown.cancel()
                    existing.teardown = None
                    logger.info(
                        f"Stream {stream_key[:6]}… reconnected within grace; "
                        f"keeping flight {existing.flight_id}"
                    )
                else:
                    # A duplicate online hook for a flight already running.
                    logger.info(
                        f"Stream {stream_key[:6]}… already live as flight "
                        f"{existing.flight_id}; ignoring"
                    )
                return existing.flight_id

            opened = await self._directory.open_flight(stream_key)
            if opened is None:
                # Revoked between MediaMTX accepting the publisher and the stream
                # going live. Nothing to run.
                logger.warning(f"No flight opened for stream {stream_key[:6]}… — key unusable")
                return None

            env = build_flight_env(opened, self._base_env)

            try:
                handle = await asyncio.to_thread(
                    self._runtime.start, opened["flight_id"], env)
            except Exception as e:
                # The flight row exists but nothing is processing it. Close it rather
                # than leaving a row that looks live forever.
                logger.error(f"Failed to start container for flight {opened['flight_id']}: {e}")
                await self._directory.close_flight(
                    opened["flight_id"], opened["publisher_token"])
                return None

            self._flights[stream_key] = Flight(
                flight_id=opened["flight_id"],
                public_uuid=opened["public_uuid"],
                handle=handle,
                publisher_token=opened["publisher_token"],
            )
            logger.info(
                f"Flight {opened['flight_id']} live for stream {stream_key[:6]}… "
                f"on {opened['output_path']}"
            )
            return opened["flight_id"]

    async def stream_offline(self, stream_key: str) -> None:
        """A publisher disconnected. Teardown is deferred by the grace period."""
        async with self._lock_for(stream_key):
            flight = self._flights.get(stream_key)
            if flight is None:
                logger.info(f"Offline for stream {stream_key[:6]}… with no flight; ignoring")
                return

            if flight.teardown is not None:
                logger.info(f"Teardown already pending for flight {flight.flight_id}")
                return

            if self._grace_s <= 0:
                await self._teardown(stream_key, flight)
                return

            flight.teardown = asyncio.create_task(
                self._teardown_after_grace(stream_key, flight),
                name=f"teardown-{flight.flight_id}")
            logger.info(
                f"Flight {flight.flight_id} offline; tearing down in {self._grace_s}s "
                "unless the stream returns"
            )

    # ── Teardown ──────────────────────────────────────────────────────────────

    async def _teardown_after_grace(self, stream_key: str, flight: Flight) -> None:
        try:
            await asyncio.sleep(self._grace_s)
        except asyncio.CancelledError:
            # Reconnected. stream_online has already cleared the task reference.
            return

        # Retake the lock: an online event may be mid-flight by now.
        async with self._lock_for(stream_key):
            # Re-read rather than trusting the captured reference — the flight may
            # have been replaced, or the cancel may have raced this far.
            current = self._flights.get(stream_key)
            if current is None or current.flight_id != flight.flight_id:
                return
            if current.teardown is None:
                # Cancelled between the sleep returning and the lock being acquired.
                return
            await self._teardown(stream_key, current)

    async def _teardown(self, stream_key: str, flight: Flight) -> None:
        logger.info(f"Tearing down flight {flight.flight_id}")

        try:
            await asyncio.to_thread(self._runtime.stop, flight.handle)
        except Exception as e:
            # Still close the flight: a stuck container must not leave the row open.
            logger.error(f"Error stopping container for flight {flight.flight_id}: {e}")

        await self._directory.close_flight(flight.flight_id, flight.publisher_token)

        self._flights.pop(stream_key, None)
        # The lock is deliberately kept. Discarding it here would be discarding a lock
        # this coroutine currently holds, and any waiter would then be handed a fresh
        # one — two operations on the same stream running at once, which is exactly
        # what the lock exists to prevent. They are bounded by distinct stream keys.
        logger.info(f"Flight {flight.flight_id} closed")

    async def shutdown(self) -> None:
        """Stop every running flight. Called when the orchestrator itself goes down."""
        for stream_key, flight in list(self._flights.items()):
            if flight.teardown is not None:
                flight.teardown.cancel()
                flight.teardown = None
            await self._teardown(stream_key, flight)
