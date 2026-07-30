"""
The authorisation decision behind Mosquitto's HTTP auth plugin (mosquitto-go-auth).

Mirrors media_auth.py exactly, one level down: Mosquitto holds no roster either.
On every CONNECT it POSTs to /auth/mqtt/user asking whether the credential is
live; on every PUBLISH and SUBSCRIBE (and again on each message actually
delivered to a subscriber) it POSTs to /auth/mqtt/acl asking whether this
identity may touch this topic. 2xx allows, anything else denies — verified
against the real plugin: acc=2 is a publish, acc=4 is a subscribe request,
acc=1 is the per-message read check that follows a successful subscribe.

Telemetry topics are namespaced by stream_key precisely so that two concurrent
flights never receive each other's telemetry — telemetry/<stream_key>/<field> —
mirroring why the video plane has in/<stream_key> and out/<public_uuid> instead
of one shared path.

There are exactly two legitimate identities, one per access level:

  publish (acc=2)             the drone          the topic's key IS the credential
  read/subscribe (acc=1|4)    the app container  publisher token for a flight
                                                  opened on THAT stream

The drone case has no separate password to check, same as MediaMTX's ingest
path: whoever holds the stream key holds the whole credential. The app case
reuses the exact same publisher token already used to authorise the raw video
read and the annotated-output publish — this is a fourth thing that one token
authorises, not a new credential.
"""

import re

from auth import AuthError, flight_id_from_credential
from constants import STREAM_KEY_ALPHABET, STREAM_KEY_LENGTH, TOKEN_SCOPE_PUBLISH


class Denied(Exception):
    """Raised for any attempt that must not be allowed. The reason is logged, never returned."""


MQTT_ACC_READ = 1
MQTT_ACC_WRITE = 2
MQTT_ACC_SUBSCRIBE = 4

TELEMETRY_FIELDS = ("latitude", "longitude", "rel_alt", "gb_yaw")

# Anchored, like INGEST_PATH in media_auth.py: a stream key MediaMTX would reject
# for the video plane must be rejected here too, for the same reason.
TELEMETRY_TOPIC = re.compile(
    rf"^telemetry/([{re.escape(STREAM_KEY_ALPHABET)}]{{{STREAM_KEY_LENGTH}}})/"
    rf"({'|'.join(TELEMETRY_FIELDS)})$"
)

STREAM_KEY = re.compile(
    rf"^[{re.escape(STREAM_KEY_ALPHABET)}]{{{STREAM_KEY_LENGTH}}}$"
)


def identify(username: str, directory) -> bool:
    """
    Called once per CONNECT. True if the username is either a live stream key
    (the drone) or a valid publisher token (the app container).

    This only proves the credential is live — it says nothing about which topic
    it may touch, which is checked again, per attempt, in authorize() below.
    Mirrors why MediaMTX's ingest-publish case in media_auth.py does not stop at
    "is this a well-formed key": liveness has to be checked at the point where it
    is actually spent.
    """
    if STREAM_KEY.fullmatch(username or ""):
        return directory.resolve_stream_key(username) is not None

    try:
        flight_id_from_credential(username, TOKEN_SCOPE_PUBLISH)
    except AuthError:
        return False
    return True


def authorize(username: str, topic: str, acc: int, directory) -> None:
    """
    Allow one publish/subscribe/read attempt, or raise Denied.

    `directory` is anything exposing resolve_stream_key / flight_stream_id — the
    real UserDirectory in the service, a stub in tests.
    """
    match = TELEMETRY_TOPIC.match(topic or "")
    if not match:
        raise Denied(f"'{topic}' is not a telemetry topic")
    stream_key = match.group(1)

    if acc == MQTT_ACC_WRITE:
        # The drone. Possession of the key is the whole credential — the only
        # question is whether it may publish under its OWN topic and is still
        # live, exactly like publishing to in/<stream_key>.
        if username != stream_key:
            raise Denied("stream key does not own this topic")
        if directory.resolve_stream_key(stream_key) is None:
            raise Denied("unknown or revoked stream key")
        return

    if acc in (MQTT_ACC_READ, MQTT_ACC_SUBSCRIBE):
        # The app container. Same comparison as reading in/<stream_key>: its
        # token names a flight, and that flight has to have been opened on the
        # very stream this topic identifies.
        stream = directory.resolve_stream_key(stream_key)
        if stream is None:
            raise Denied("unknown or revoked stream key")

        try:
            flight_id = flight_id_from_credential(username, TOKEN_SCOPE_PUBLISH)
        except AuthError as e:
            raise Denied(str(e))

        if directory.flight_stream_id(flight_id) != stream["stream_id"]:
            raise Denied(f"flight {flight_id} was not opened on this stream")
        return

    raise Denied(f"access level {acc} is not authorised here")
