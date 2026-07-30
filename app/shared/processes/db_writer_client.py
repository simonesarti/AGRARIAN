import base64
import logging
from datetime import datetime
from typing import Optional

import requests
from requests.exceptions import RequestException


logger = logging.getLogger("main.alert_out.db_client")


class DbWriterClient:
    """
    HTTP client for the db-writer sidecar service.

    Holds no DB credentials — every privileged operation is delegated to the sidecar
    over the internal network. It holds no *end-user* credential either: the flight
    already exists by the time this container starts, so there is nothing to
    authenticate and no login step. bind_flight() is a local assignment, not a
    request.

    This is what changed with the orchestrator. The client used to POST
    /session/start with the operator's email and password and open its own flight;
    now the orchestrator does that from the stream key MediaMTX gave it, and injects
    only the flight_id and a token scoped to that one flight.
    """

    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self.flight_id: Optional[int] = None
        self._publisher_token: Optional[str] = None

    @property
    def publisher_token(self) -> Optional[str]:
        """Shared with WsServerClient — both write paths accept the same token."""
        return self._publisher_token

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._publisher_token}"}

    def bind_flight(self, flight_id: int, publisher_token: str) -> None:
        """
        Scope every subsequent write to this flight, with the credential that
        authorises it. Both values come from the orchestrator's environment.
        """
        if not flight_id or not publisher_token:
            raise ValueError(
                "bind_flight requires both a flight_id and a publisher token — "
                "without them an alert has no owner and no authorisation."
            )
        self.flight_id = flight_id
        self._publisher_token = publisher_token
        logger.info(f"DB client bound to flight_id={flight_id}")

    def save_alert(
        self,
        frame_id: int,
        alert_msg: str,
        timestamp: float,
        datetime: datetime,
        image_data: Optional[bytes],
        image_width: int,
        image_height: int,
    ) -> bool:
        if self.flight_id is None:
            return False
        try:
            payload = {
                "frame_id": frame_id,
                "alert_msg": alert_msg,
                "timestamp": timestamp,
                "datetime": datetime.isoformat(),
                "image_data": base64.b64encode(image_data).decode() if image_data else None,
                "image_width": image_width,
                "image_height": image_height,
            }
            resp = requests.post(
                f"{self._base}/session/{self.flight_id}/alert",
                json=payload,
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return True
        except RequestException as e:
            logger.error(f"Failed to save alert (frame {frame_id}): {e}")
            return False

    def close(self) -> None:
        """
        Drop the credential. Local only — closing the FLIGHT is not this container's
        call to make.

        The orchestrator stops this container because the publisher went offline, and
        then stamps end_time itself. If the app also closed the flight it would race
        the reconnect grace period: a drone recovering from a radio glitch inside the
        window keeps its flight, and a container that closed it on the way down would
        have marked that same flight finished.
        """
        self.flight_id = None
        self._publisher_token = None
