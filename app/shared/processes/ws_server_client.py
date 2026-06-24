import logging
from typing import Optional

import requests
from requests.exceptions import RequestException


logger = logging.getLogger("main.alert_out.ws_client")


class WsServerClient:
    """
    HTTP client for the ws-server sidecar service.
    Mirrors the queue_alert() interface of WebSocketManager but delegates the
    actual broadcasting to the sidecar over the internal Docker network.
    """

    def __init__(self, base_url: str, timeout: float = 3.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def send_alert(self, alert_data: dict) -> bool:
        """
        POST an alert payload to the ws-server sidecar for broadcasting.
        Returns True on success, False on network / HTTP error (non-raising).
        """
        try:
            resp = requests.post(
                f"{self._base}/alert",
                json=alert_data,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return True
        except RequestException as e:
            logger.error(f"Failed to send alert to ws-server: {e}")
            return False

    def close(self) -> None:
        """No persistent connection to close; present for interface symmetry."""
        pass
