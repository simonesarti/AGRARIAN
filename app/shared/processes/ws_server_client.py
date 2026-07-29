import logging
from typing import Optional

import requests
from requests.exceptions import RequestException


logger = logging.getLogger("main.alert_out.ws_client")


class WsServerClient:
    """
    HTTP client for the ws-server sidecar service.
    Mirrors the queue_alert() interface of WebSocketManager but delegates the
    actual broadcasting to the sidecar.

    Alerts are scoped to a flight: bind_flight() must be called with the flight_id
    and publisher token issued by db-writer before any alert can be sent. Without
    them there is no way to tell ws-server which viewers may see the alert, so
    sending is refused rather than broadcast to everyone.

    The token is the same one db-writer issued for this flight — both write paths
    accept it, and it authorises this flight only.
    """

    def __init__(self, base_url: str, timeout: float = 3.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._publisher_token: Optional[str] = None
        self.flight_id: Optional[int] = None

    def bind_flight(self, flight_id: int, publisher_token: str) -> None:
        """Scope every subsequent alert to this flight, with its own credential."""
        self.flight_id = flight_id
        self._publisher_token = publisher_token
        logger.info(f"WS client bound to flight_id={flight_id}")

    def send_alert(self, alert_data: dict) -> bool:
        """
        POST an alert payload to the ws-server sidecar for broadcasting.
        Returns True on success, False on network / HTTP error (non-raising).
        """
        if self.flight_id is None or self._publisher_token is None:
            logger.error("No flight bound — alert not sent (would have no audience scope)")
            return False

        try:
            resp = requests.post(
                f"{self._base}/session/{self.flight_id}/alert",
                json=alert_data,
                headers={"Authorization": f"Bearer {self._publisher_token}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return True
        except RequestException as e:
            logger.error(f"Failed to send alert to ws-server: {e}")
            return False

    def close(self) -> None:
        """No persistent connection to close; present for interface symmetry."""
        self.flight_id = None
        self._publisher_token = None
