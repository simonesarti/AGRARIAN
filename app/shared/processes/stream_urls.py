"""
Media-server URL composition, and redaction of the credential it carries.

These two belong in one file because they must not drift apart: the moment a stream
URL carries a bearer credential, every log line and exception message that prints a
URL becomes a credential disclosure.

**Why the token goes in the query string.** The app presents its per-flight publisher
token to MediaMTX twice — reading the drone's ingest path and publishing the annotated
output. RTSP userinfo (`rtsp://x:<token>@host/in/<key>`) was tried first and does not
work: MediaMTX challenges the client, FFmpeg answers with a digest, and the auth hook
is handed something that is not the JWT at all — it fails with "Not enough segments".
The query string is passed through verbatim on both RTSP and RTMP and arrives in the
`query` field MediaMTX POSTs to the hook, which is what db-writer's credential_from()
reads. Verified against MediaMTX v1.19.3 for rtsp read, rtmp read and rtmp publish.

Consequence: no URL produced by build_stream_url() may be logged as-is.
"""

import re
from typing import Optional
from urllib.parse import quote


# Query fields db-writer's credential_from() will accept as a credential, plus the
# obvious neighbours. Kept a superset on purpose: this list decides what gets hidden,
# so erring wide costs nothing and erring narrow leaks a token.
_QUERY_CREDENTIAL = re.compile(
    r"([?&](?:token|jwt|pass|password|secret|key)=)[^&]*",
    re.IGNORECASE,
)

# userinfo form, e.g. rtsp://user:secret@host/path
_USERINFO_PASSWORD = re.compile(r"(://[^/@]*:)[^/@]*(@)")

_REDACTED = "***"


def build_stream_url(
    protocol: str,
    host: str,
    port: int,
    path: str,
    token: Optional[str] = None,
) -> str:
    """
    Compose PROTOCOL://HOST:PORT/PATH[?token=...].

    `path` is a media-server path such as `in/<stream_key>` or `out/<public_uuid>`,
    with or without a leading slash. The token is percent-encoded: a JWT is
    URL-safe base64 so in practice nothing changes, but this must not depend on
    the token format staying that way.
    """
    url = f"{protocol}://{host}:{port}/{path.lstrip('/')}"
    if token:
        url = f"{url}?token={quote(token, safe='')}"
    return url


def redact_stream_url(url: Optional[str]) -> str:
    """
    The same URL with any embedded credential replaced by ***, safe to log.

    Everything else is preserved, because the point of logging a URL is to see which
    host, port and path were used — that is exactly what stays.
    """
    if not url:
        return ""
    url = _QUERY_CREDENTIAL.sub(rf"\1{_REDACTED}", url)
    return _USERINFO_PASSWORD.sub(rf"\1{_REDACTED}\2", url)
