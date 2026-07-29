import asyncio
import json
import logging
from typing import Dict, Optional, Set
from urllib.parse import parse_qs, urlparse

import redis.asyncio as redis
import websockets
from websockets.server import serve

from auth import AuthError, flight_id_from_token
from constants import (
    REDIS_CHANNEL_PREFIX,
    REDIS_POLL_TIMEOUT,
    REDIS_RETRY_DELAY,
    REDIS_URL,
    WS_CLOSE_UNAUTHORIZED,
    WS_HOST,
    WS_MANAGER_BROADCAST_TIMEOUT,
    WS_MANAGER_PING_INTERVAL,
    WS_MANAGER_PING_TIMEOUT,
    WS_PORT,
)

logger = logging.getLogger("ws_server.manager")


def _token_from(websocket) -> Optional[str]:
    """
    Pull ?token= out of the handshake path.

    Query string rather than a header because browsers cannot set headers on a
    WebSocket handshake. The trade-off is that the token reaches proxy access
    logs, which is why these JWTs are minted short-lived.
    """
    # websockets >= 14 exposes the handshake on .request; older versions on .path
    request = getattr(websocket, "request", None)
    path = request.path if request is not None else getattr(websocket, "path", "")
    values = parse_qs(urlparse(path).query).get("token")
    return values[0] if values else None


class WebSocketManager:
    """
    Per-flight alert fan-out.

    Every alert belongs to exactly one flight, and a viewer's JWT names exactly
    one flight, so a socket only ever receives its own tenant's alerts.

    Fan-out goes through Redis so the service can run more than one replica: an
    app pod may POST to any replica while its viewers are connected to others.
    Each replica subscribes only to the flights it currently has viewers for, so
    a tenant's frames are never shipped to replicas with nobody waiting on them.

    Single event loop throughout — the server, the Redis reader, and the client
    handlers all run as tasks on the loop uvicorn already owns, so the session
    map needs no locking.
    """

    def __init__(
            self,
            host: str = WS_HOST,
            port: int = WS_PORT,
            redis_url: str = REDIS_URL,
            ping_interval: float = WS_MANAGER_PING_INTERVAL,
            ping_timeout: float = WS_MANAGER_PING_TIMEOUT,
            broadcast_timeout: float = WS_MANAGER_BROADCAST_TIMEOUT,
            poll_timeout: float = REDIS_POLL_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.redis_url = redis_url
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.broadcast_timeout = broadcast_timeout
        self.poll_timeout = poll_timeout

        # flight_id → sockets watching that flight *on this replica*
        self._sessions: Dict[int, Set] = {}

        self._redis: Optional[redis.Redis] = None
        self._pubsub = None
        self._server = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def connected_clients(self) -> int:
        return sum(len(v) for v in self._sessions.values())

    @property
    def active_flights(self) -> int:
        return len(self._sessions)

    # ── Redis keys ────────────────────────────────────────────────────────────

    def _channel(self, flight_id: int) -> str:
        return f"{REDIS_CHANNEL_PREFIX}:{flight_id}"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self._stop_event = asyncio.Event()

        self._redis = redis.from_url(self.redis_url, decode_responses=True)
        await self._redis.ping()
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)

        self._server = await serve(
            self._handle_client,
            self.host,
            self.port,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
        )
        self._reader_task = asyncio.create_task(self._redis_reader(), name="redis-reader")
        logger.info(f"WebSocket server active on ws://{self.host}:{self.port}")

    async def stop(self):
        if self._stop_event:
            self._stop_event.set()

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                logger.info("Redis reader task cancelled")

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("WebSocket server closed")

        if self._pubsub:
            await self._pubsub.aclose()
        if self._redis:
            await self._redis.aclose()
        logger.info("Redis connections closed")

    # ── Publish side (app pods) ───────────────────────────────────────────────

    async def publish_alert(self, flight_id: int, alert_data: Dict):
        """
        Hand an alert to Redis for delivery to every replica holding a viewer of
        this flight. Returns once Redis has accepted it, not once viewers have
        it — delivery is best-effort by design, alerts are not replayed.
        """
        message = json.dumps(alert_data)

        replicas = await self._redis.publish(self._channel(flight_id), message)

        logger.info(
            f"Alert published: flight={flight_id} "
            f"frame={alert_data.get('frame_id')} replicas={replicas}"
        )

    # ── Subscribe side (viewers) ──────────────────────────────────────────────

    async def _register(self, flight_id: int, websocket):
        viewers = self._sessions.get(flight_id)
        if viewers is None:
            viewers = set()
            self._sessions[flight_id] = viewers
            # First viewer for this flight here — start pulling its alerts.
            await self._pubsub.subscribe(self._channel(flight_id))
            logger.debug(f"Subscribed to {self._channel(flight_id)}")
        viewers.add(websocket)

    async def _unregister(self, flight_id: int, websocket):
        viewers = self._sessions.get(flight_id)
        if viewers is None:
            return
        viewers.discard(websocket)
        if not viewers:
            del self._sessions[flight_id]
            # Last viewer gone — stop paying to receive this flight's frames.
            await self._pubsub.unsubscribe(self._channel(flight_id))
            logger.debug(f"Unsubscribed from {self._channel(flight_id)}")

    async def _handle_client(self, websocket):
        """Authorise a viewer, then hold the connection open for its flight."""
        client_addr = websocket.remote_address

        try:
            flight_id = flight_id_from_token(_token_from(websocket))
        except AuthError as e:
            logger.warning(f"Rejected viewer {client_addr}: {e}")
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="unauthorized")
            return

        await self._register(flight_id, websocket)
        logger.info(
            f"Viewer connected: {client_addr} flight={flight_id}. "
            f"Flight total: {len(self._sessions[flight_id])}"
        )

        try:
            # No replay on connect. A viewer sees only alerts raised while it is
            # watching: an alert describes a moment in a live flight, and showing a
            # stale one on a fresh connection would assert something about the field
            # that may no longer be true. A blank screen is the honest initial state.
            # Past alerts are in the database, where they carry their timestamp.

            # Keep the connection alive; the library handles ping/pong itself.
            async for message in websocket:
                logger.debug(f"Ignoring unexpected message from {client_addr}: {message[:50]}")

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Connection closed by {client_addr}")
        except Exception as e:
            logger.error(f"Error handling viewer {client_addr}: {e}", exc_info=True)
        finally:
            await self._unregister(flight_id, websocket)
            logger.info(f"Viewer {client_addr} disconnected from flight {flight_id}")

    # ── Fan-out ───────────────────────────────────────────────────────────────

    async def _redis_reader(self):
        """Pump alerts off Redis and onto the local sockets that want them."""
        logger.info("Redis reader started")

        while not self._stop_event.is_set():
            try:
                # get_message() raises if the pubsub holds no subscriptions, which
                # is the normal state whenever no viewer is connected here.
                if not self._pubsub.subscribed:
                    await asyncio.sleep(self.poll_timeout)
                    continue

                message = await self._pubsub.get_message(timeout=self.poll_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Redis reader error: {e}", exc_info=True)
                await asyncio.sleep(REDIS_RETRY_DELAY)
                continue

            if message is None:
                continue

            try:
                flight_id = int(str(message["channel"]).split(":")[1])
            except (KeyError, IndexError, ValueError):
                logger.warning(f"Dropping message on unparseable channel: {message.get('channel')}")
                continue

            await self._fan_out(flight_id, message["data"])

        logger.info("Redis reader stopped")

    async def _fan_out(self, flight_id: int, message: str):
        viewers = list(self._sessions.get(flight_id, ()))
        if not viewers:
            return

        tasks = [asyncio.create_task(v.send(message)) for v in viewers]
        done, pending = await asyncio.wait(tasks, timeout=self.broadcast_timeout)
        for task in pending:
            # A stalled viewer must not hold up the others or the reader loop.
            task.cancel()

        logger.info(
            f"Alert delivered to {len(done)}/{len(viewers)} viewer(s) of flight {flight_id}"
        )
