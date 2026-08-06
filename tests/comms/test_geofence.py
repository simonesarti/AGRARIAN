"""
Per-slot geofence, from the column to the container's environment.

The operating boundary used to be ONE deployment-wide polygon, so every tenant on a
deployment was evaluated against the same fence and at least one of them got wrong
danger calls on their own land. Nothing leaked — the app is per-flight and sole
occupant, which is what §4 asserts — but the answer it computed belonged to somebody
else. That is a correctness defect rather than a disclosure one, and it is the reason
this moved.

Three groups:

  validation   longitude/latitude ranges, the three-point floor, the ceiling, and the
               ORDER, which is the one a map UI will get wrong: longitude first.
  ownership    another tenant's stream_id answers exactly as an absent one does and
               leaves the owner's fence untouched.
  injection    the stored JSON is rendered into the "(lon, lat), ..." spelling the app
               already parses, and NO fence injects nothing rather than an empty
               variable — which is what keeps a deployment that never configures one
               behaving exactly as it did.

Run with no stack:

  docker run --rm -v "$PWD/db_writer:/w:ro" -v "$PWD/orchestrator:/o:ro" \
    -v "$PWD/tests/comms:/tests:ro" -w /tmp -e DB_WRITER_DIR=/w -e ORCHESTRATOR_DIR=/o \
    python:3.11-slim \
    sh -c "pip install -q sqlalchemy bcrypt; python /tests/test_geofence.py"
"""

import json
import os
import sys

sys.path.insert(0, os.environ["DB_WRITER_DIR"])

import db_manager as m
from db_manager import UserDirectory, StreamNotFound, geofence_to_env
from sqlalchemy import create_engine

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))


d = UserDirectory.__new__(UserDirectory)
d._engine = create_engine("sqlite:///:memory:")
m.Base.metadata.create_all(d._engine)

alice = d.create_user("a@x.com", "password123")["user_id"]
bob = d.create_user("b@x.com", "password123")["user_id"]

SQUARE = [[11.0, 45.0], [11.1, 45.0], [11.1, 45.1], [11.0, 45.1]]

# ── creation ──────────────────────────────────────────────────────────────────
s_none = d.create_stream(alice, "no fence")
check("a slot may have no fence at all", s_none["geofence"] is None)

s_fenced = d.create_stream(alice, "north field", None, SQUARE)
check("a slot may carry a boundary", s_fenced["geofence"] == SQUARE, str(s_fenced["geofence"]))

check("an empty list is the same as none",
      d.create_stream(alice, "empty", None, [])["geofence"] is None)

# ── validation ────────────────────────────────────────────────────────────────
bad = {
    "two points is not a polygon": [[11.0, 45.0], [11.1, 45.0]],
    "longitude out of range": [[181.0, 45.0], [11.1, 45.0], [11.1, 45.1]],
    "latitude out of range": [[11.0, 91.0], [11.1, 45.0], [11.1, 45.1]],
    "longitude below range": [[-181.0, 45.0], [11.1, 45.0], [11.1, 45.1]],
    "a bare number is not a pair": [11.0, 45.0, 46.0],
    "a string is not a pair list": "11,45 11.1,45 11.1,45.1",
    "three values in a point": [[11.0, 45.0, 3.0], [11.1, 45.0], [11.1, 45.1]],
    "not a number": [["north", 45.0], [11.1, 45.0], [11.1, 45.1]],
    "too many points": [[11.0, 45.0]] * 500,
}
for name, value in bad.items():
    try:
        d.create_stream(alice, "bad", None, value)
        check(f"refused: {name}", False)
    except ValueError:
        check(f"refused: {name}", True)

# The one a map UI gets wrong. (45, 11) is a valid PAIR but the wrong ORDER, and
# because latitude is bounded tighter than longitude it is caught only when the
# latitude value exceeds 90 — so this is asserted rather than assumed.
try:
    d.create_stream(alice, "swapped", None, [[45.0, 111.0], [45.1, 111.0], [45.1, 111.1]])
    check("lat/lon swapped is caught when latitude exceeds 90", False)
except ValueError:
    check("lat/lon swapped is caught when latitude exceeds 90", True)

# ── ownership ─────────────────────────────────────────────────────────────────
try:
    d.set_stream_geofence(s_fenced["stream_id"], bob, [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0]])
    check("another tenant cannot set a fence", False)
except StreamNotFound:
    check("another tenant cannot set a fence", True)

listed = {s["stream_id"]: s for s in d.list_streams(alice)}
check("...and the owner's fence is untouched by that attempt",
      listed[s_fenced["stream_id"]]["geofence"] == SQUARE)

try:
    d.set_stream_geofence(999999, alice, SQUARE)
    check("a stream that does not exist is the same refusal", False)
except StreamNotFound:
    check("a stream that does not exist is the same refusal", True)

try:
    d.set_stream_geofence(s_none["stream_id"], alice, [[1.0, 1.0]])
    check("a bad fence on an owned slot is NOT StreamNotFound", False)
except StreamNotFound:
    check("a bad fence on an owned slot is NOT StreamNotFound", False, "HTTP would 404 a 400")
except ValueError:
    check("a bad fence on an owned slot is NOT StreamNotFound", True)

# ── changing and clearing ─────────────────────────────────────────────────────
TRIANGLE = [[9.0, 44.0], [9.5, 44.0], [9.25, 44.5]]
d.set_stream_geofence(s_none["stream_id"], alice, TRIANGLE)
check("a fence can be added to a slot that had none",
      {s["stream_id"]: s for s in d.list_streams(alice)}[s_none["stream_id"]]["geofence"] == TRIANGLE)

d.set_stream_geofence(s_none["stream_id"], alice, None)
check("...and cleared again, which disables geofencing",
      {s["stream_id"]: s for s in d.list_streams(alice)}[s_none["stream_id"]]["geofence"] is None)

# ── rendering into the app's spelling ─────────────────────────────────────────
rendered = geofence_to_env(json.dumps(SQUARE))
check("rendered in the form the app's parser reads",
      rendered == "(11.0, 45.0), (11.1, 45.0), (11.1, 45.1), (11.0, 45.1)", rendered)
check("no fence renders as nothing, not an empty string", geofence_to_env(None) is None)

# The app's own parser must accept what this produces. Its regex is reproduced here
# rather than imported, because the app tree is not on this path — if the two ever
# disagree, this is where it shows.
import re
pairs = re.findall(r"\(\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*\)", rendered)
check("...and the app's own regex parses it back to the same points",
      [[float(a), float(b)] for a, b in pairs] == SQUARE, str(pairs))

# ── injection ─────────────────────────────────────────────────────────────────
sys.path.insert(0, os.environ["ORCHESTRATOR_DIR"])
from flights import build_flight_env

opened = d.open_flight_for_key(s_fenced["stream_key"])
check("open_flight_for_key carries the rendered fence",
      opened["geofence_vertexes"] == rendered, str(opened["geofence_vertexes"]))
opened["publisher_token"] = "tok"
env = build_flight_env(opened, {"DB_WRITER_URL": "x"}, s_fenced["stream_key"])
check("the container is given GEOFENCING_VERTEXES", env.get("GEOFENCING_VERTEXES") == rendered)

opened_none = d.open_flight_for_key(s_none["stream_key"])
opened_none["publisher_token"] = "tok"
env_none = build_flight_env(opened_none, {"DB_WRITER_URL": "x"}, s_none["stream_key"])
check("a slot with no fence injects nothing at all", "GEOFENCING_VERTEXES" not in env_none)

# The control for the line above: an empty string in the environment is NOT the same
# as an absent variable — env_ignore_empty makes the app treat it as unset today, but
# that is a setting somebody could change, and absent is unambiguous.
check("...and specifically not an empty string",
      env_none.get("GEOFENCING_VERTEXES", None) is None)

# Two tenants, two fences, no bleed — the defect this whole change exists to fix.
b_stream = d.create_stream(bob, "bob's field", None, TRIANGLE)
b_opened = d.open_flight_for_key(b_stream["stream_key"])
check("two tenants' flights carry two different fences",
      b_opened["geofence_vertexes"] != opened["geofence_vertexes"],
      f"{b_opened['geofence_vertexes']} vs {opened['geofence_vertexes']}")

print()
print("=" * 60)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
