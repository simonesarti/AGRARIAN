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
    Mirrors the DatabaseManager interface but holds no DB credentials —
    all privileged operations are delegated to the sidecar over the
    internal Docker network.
    """

    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self.flight_id: Optional[int] = None
        self.public_uuid: Optional[str] = None
        # Scoped to this flight alone. Issued by /session/start and presented on every
        # subsequent write, so a leak cannot be used against any other flight.
        self._publisher_token: Optional[str] = None
        self.viewer_token: Optional[str] = None

    @property
    def publisher_token(self) -> Optional[str]:
        """Shared with WsServerClient — both write paths accept the same token."""
        return self._publisher_token

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._publisher_token}"}

    def initialize(self, username: str, password: str) -> None:
        """
        Authenticate the user via the sidecar and create a new flight record.
        Sets self.flight_id and the flight's publisher token on success;
        raises on auth failure or network error.
        """
        resp = requests.post(
            f"{self._base}/session/start",
            json={
                "email": username,
                "password": password,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        self.flight_id = body["flight_id"]
        self.public_uuid = body.get("public_uuid")
        self._publisher_token = body["publisher_token"]
        self.viewer_token = body.get("viewer_token")
        logger.info(f"Session started, flight_id={self.flight_id}")

    def set_stream_url(self, url: str) -> bool:
        if self.flight_id is None:
            return False
        try:
            resp = requests.post(
                f"{self._base}/session/{self.flight_id}/stream-url",
                json={"url": url},
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return True
        except RequestException as e:
            logger.error(f"Failed to set stream URL for flight {self.flight_id}: {e}")
            return False

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
        if self.flight_id is None:
            return
        try:
            requests.delete(
                f"{self._base}/session/{self.flight_id}",
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except RequestException as e:
            logger.error(f"Failed to close session {self.flight_id}: {e}")
        finally:
            self.flight_id = None
            self._publisher_token = None
