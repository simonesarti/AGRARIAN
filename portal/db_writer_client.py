"""
The portal's view of db-writer.

The portal holds no database credentials and no signing secret (§7). It cannot
validate a session token, only carry one: every request here forwards the cookie's
value as a bearer token and lets db-writer decide. That is the whole reason the
browser never reaches db-writer directly and the reason this file exists.

Failures are turned into three kinds, because the portal reacts to them in three
different ways:

  SessionExpired      the credential is gone or bad   → log out, back to /login
  UpstreamRejected    the user asked for something    → re-render the page with why
                      db-writer refused
  DbWriterUnavailable db-writer is not answering      → 502, nothing to retry here
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger("portal.db_writer")


class SessionExpired(Exception):
    """db-writer answered 401: the session token is missing, malformed or expired."""


class UpstreamRejected(Exception):
    """
    db-writer refused the request for a reason the user can act on.

    `detail` is db-writer's own message and is rendered back to the browser, so it
    must stay free of anything a tenant should not see. The routes that raise it
    are the account-scoped ones, whose refusals are already written for a human:
    "That email is already registered", "Password must be at least 8 characters".
    """

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class DbWriterUnavailable(Exception):
    """db-writer could not be reached at all."""


def _detail_of(resp: httpx.Response) -> str:
    """FastAPI's error shape is {"detail": ...}; fall back to the raw body."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200] or f"HTTP {resp.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        # /viewer/token's 409 is a structured detail, not a string.
        return str(detail.get("message", detail))
    return str(detail) if detail else f"HTTP {resp.status_code}"


class DbWriterClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    async def _call(
        self,
        method: str,
        path: str,
        json: Optional[dict] = None,
        token: Optional[str] = None,
    ) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(
                    method, f"{self._base}{path}", json=json, headers=headers)
        except httpx.HTTPError as e:
            logger.error(f"db-writer unreachable on {method} {path}: {e}")
            raise DbWriterUnavailable(str(e))

        if resp.status_code == 401:
            raise SessionExpired(_detail_of(resp))
        if resp.status_code >= 400:
            raise UpstreamRejected(resp.status_code, _detail_of(resp))
        return resp.json() if resp.content else {}

    # ── Account ───────────────────────────────────────────────────────────────

    async def register(self, email: str, password: str) -> dict:
        """
        Returns the new account and a session token. No separate login follows:
        the password was proven in this same request (see db-writer's /register).

        A 401 is impossible here, so SessionExpired would mean db-writer changed
        under us; it is left to propagate rather than papered over.
        """
        return await self._call(
            "POST", "/register", json={"email": email, "password": password})

    async def login(self, email: str, password: str) -> dict:
        return await self._call(
            "POST", "/login", json={"email": email, "password": password})

    async def whoami(self, token: str) -> dict:
        return await self._call("GET", "/me", token=token)

    # ── Stream slots ──────────────────────────────────────────────────────────
    #
    # None of these takes a user_id. The session token is the identity, resolved
    # by db-writer from the claim — a user_id travelling in a portal request would
    # be a number the browser could change.

    async def list_streams(self, token: str) -> list:
        return (await self._call("GET", "/streams", token=token))["streams"]

    async def create_stream(self, token: str, label: Optional[str]) -> dict:
        return await self._call("POST", "/streams", json={"label": label}, token=token)

    async def rotate_stream(self, token: str, stream_id: int) -> dict:
        return await self._call("POST", f"/streams/{stream_id}/rotate", token=token)

    async def revoke_stream(self, token: str, stream_id: int) -> dict:
        return await self._call("POST", f"/streams/{stream_id}/revoke", token=token)

    # ── Flights ───────────────────────────────────────────────────────────────

    async def active_flights(self, token: str) -> list:
        return (await self._call("GET", "/flights", token=token))["flights"]

    async def viewer_token(self, token: str, stream_id: Optional[int]) -> dict:
        """
        Trade the account-scoped session token for a flight-scoped viewer token.

        A downgrade with no path back (§3): what comes out can watch one flight
        and do nothing else, which is what makes it safe to hand to page
        JavaScript and put in a URL — unlike the session token it was minted
        from, which stays in the httpOnly cookie.
        """
        return await self._call(
            "POST", "/viewer/token", json={"stream_id": stream_id}, token=token)
