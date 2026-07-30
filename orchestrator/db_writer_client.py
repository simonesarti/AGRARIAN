"""
The orchestrator's view of db-writer.

The orchestrator holds no database credentials and mints no tokens. db-writer is the
only service that talks to the database and the only one that signs tokens, so keeping
that true means the orchestrator asks rather than reaches — even though it would be
convenient to open a flight row directly.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger("orchestrator.db_writer")


class DbWriterClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    async def open_flight(self, stream_key: str) -> Optional[dict]:
        """
        Open a flight for this stream key, or None if the key is unusable.

        None rather than an exception for 401: a key revoked between MediaMTX
        accepting the publisher and the stream going live is a race, not a fault.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base}/flight/open", json={"stream_key": stream_key})
        except httpx.HTTPError as e:
            logger.error(f"db-writer unreachable while opening a flight: {e}")
            return None

        if resp.status_code == 401:
            return None
        if resp.status_code != 200:
            logger.error(f"db-writer refused to open a flight: {resp.status_code} {resp.text}")
            return None

        return resp.json()

    async def close_flight(self, flight_id: int, publisher_token: str) -> bool:
        """
        Stamp the flight finished. Failure is logged, never raised: the container is
        already stopped by this point, and the alternative to an unclosed row is an
        orchestrator that crashes during teardown.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base}/flight/{flight_id}/close",
                    headers={"Authorization": f"Bearer {publisher_token}"})
        except httpx.HTTPError as e:
            logger.error(f"db-writer unreachable while closing flight {flight_id}: {e}")
            return False

        if resp.status_code != 200:
            logger.error(
                f"db-writer refused to close flight {flight_id}: "
                f"{resp.status_code} {resp.text}")
            return False
        return True
