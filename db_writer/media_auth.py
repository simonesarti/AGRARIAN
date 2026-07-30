"""
The authorisation decision behind MediaMTX's HTTP auth hook.

MediaMTX holds no roster. On every connection attempt — publish or read, every
protocol — it POSTs the attempt here and obeys the answer: 2xx allows, anything
else denies. That is what makes user 101 work while 100 people are streaming,
with no config reload and no restart.

Kept out of main.py and free of FastAPI so the decision can be tested directly
against a stub directory, with no database and no HTTP server.

There are exactly four legitimate combinations of action and path, and each has a
different credential. Anything else is denied.

  publish  in/<stream_key>    the drone         the path IS the credential
  read     in/<stream_key>    the app container publisher token for a flight
                                                opened on THAT stream
  publish  out/<public_uuid>  the app container publisher token for THAT flight
  read     out/<public_uuid>  the viewer        viewer token for THAT flight

The ingest path carries its own credential because the operator types it into a
drone controller by hand and has nowhere to put a second one. That is only safe
because no viewer ever touches an ingest path: viewers read out/<public_uuid>,
which is unrelated to the key. Unifying the two would leak every stream key to
every viewer.
"""

import logging
import re
from typing import Optional
from urllib.parse import parse_qs

from auth import AuthError, flight_id_from_credential
from constants import (
    MEDIAMTX_ACTION_PUBLISH,
    MEDIAMTX_READ_ACTIONS,
    STREAM_KEY_ALPHABET,
    STREAM_KEY_LENGTH,
    TOKEN_SCOPE_PUBLISH,
    TOKEN_SCOPE_VIEW,
)

logger = logging.getLogger("db_writer.media_auth")


class Denied(Exception):
    """Raised for any attempt that must not be allowed. The reason is logged, never returned."""


# Built from the generator's own alphabet and length so the two cannot drift apart:
# a key MediaMTX would reject is a key that must never have been minted.
INGEST_PATH = re.compile(
    rf"^in/([{re.escape(STREAM_KEY_ALPHABET)}]{{{STREAM_KEY_LENGTH}}})$"
)

# uuid4 as str(): 8-4-4-4-12 lowercase hex. Anchored, so no prefix or suffix smuggling.
OUTPUT_PATH = re.compile(
    r"^out/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def credential_from(user: str, password: str, token: str, query: str) -> Optional[str]:
    """
    Pull the bearer credential out of a MediaMTX auth payload.

    MediaMTX delivers it in a different field per protocol, and none of them is
    reliably populated, so all four are tried:

      token     WebRTC/HLS Authorization: Bearer <jwt>
      password  RTSP/RTMP url with user:pass, or ?pass=
      query     ?token=<jwt> on RTSP/RTMP urls

    `user` is accepted for signature symmetry but never used as a credential — the
    JWT alone carries authority, so a username would be decoration that implies a
    check nobody performs.
    """
    if token:
        return token
    if password:
        return password

    if query:
        params = parse_qs(query.lstrip("?"))
        for field in ("token", "jwt", "pass"):
            values = params.get(field)
            if values and values[0]:
                return values[0]

    return None


def _flight_for(credential: Optional[str], scope: str) -> int:
    try:
        return flight_id_from_credential(credential, scope)
    except AuthError as e:
        raise Denied(str(e))


def authorize(action: str, path: str, credential: Optional[str], directory) -> None:
    """
    Allow the attempt, or raise Denied.

    `directory` is anything exposing resolve_stream_key / resolve_public_uuid /
    flight_stream_id — the real UserDirectory in the service, a stub in tests.
    """
    ingest = INGEST_PATH.match(path or "")
    output = OUTPUT_PATH.match(path or "")

    if action == MEDIAMTX_ACTION_PUBLISH:
        if ingest:
            # The drone. Possession of the key is the whole credential, so the only
            # question is whether the key is live. A revoked key resolves to nothing.
            if directory.resolve_stream_key(ingest.group(1)) is None:
                raise Denied("unknown or revoked stream key")
            return

        if output:
            # The app container republishing its annotated video. Its token names a
            # flight; the path names a flight. They must be the same flight.
            flight_id = _flight_for(credential, TOKEN_SCOPE_PUBLISH)
            if directory.resolve_public_uuid(output.group(1)) != flight_id:
                raise Denied(f"publisher token for flight {flight_id} does not own this output path")
            return

        raise Denied("publishing is only allowed on in/<stream_key> or out/<public_uuid>")

    if action in MEDIAMTX_READ_ACTIONS:
        if output:
            # A viewer. Same comparison as above, with the other scope — which is the
            # only thing stopping a viewer token from being a publisher token here.
            flight_id = _flight_for(credential, TOKEN_SCOPE_VIEW)
            if directory.resolve_public_uuid(output.group(1)) != flight_id:
                raise Denied(f"viewer token for flight {flight_id} does not grant this output path")
            return

        if ingest:
            # The app container pulling the raw feed. Its token names a flight, and
            # that flight has to have been opened on the very stream this key
            # identifies — otherwise any live publisher token would open any drone's
            # raw video.
            stream = directory.resolve_stream_key(ingest.group(1))
            if stream is None:
                raise Denied("unknown or revoked stream key")

            flight_id = _flight_for(credential, TOKEN_SCOPE_PUBLISH)
            if directory.flight_stream_id(flight_id) != stream["stream_id"]:
                raise Denied(f"flight {flight_id} was not opened on this stream")
            return

        raise Denied("reading is only allowed on in/<stream_key> or out/<public_uuid>")

    raise Denied(f"action '{action}' is not authorised here")
