"""
Named geofences, slot assignment, and the snapshot that keeps history honest.

The operating boundary used to be ONE deployment-wide polygon, so every tenant was
evaluated against the same fence and at least one of them got wrong danger calls on
their own land. Nothing leaked — the app is per-flight and sole occupant, which is what
§4 asserts — but the answer it computed belonged to somebody else. A correctness defect
rather than a disclosure one, and the more dangerous shape: a wrong answer raises
nothing, so the first sign would have been a tenant disputing an alert.

Boundaries are now named rows a user owns and slots point at. Four groups:

  validation   ranges, the three-point floor, the ceiling, and the ORDER a map UI
               invites getting wrong: longitude first.
  ownership    two sequential ids are in play — stream_id and geofence_id — and a
               guess at either must learn nothing and change nothing.
  snapshot     the property that makes a named fence safe to edit or delete at all:
               a flight records what it was JUDGED against, not a pointer to whatever
               that boundary looks like today.
  injection    the stored JSON is rendered into the "(lon, lat), ..." spelling the app
               already parses, and no fence injects nothing rather than an empty
               variable.

Run with no stack:

  docker run --rm -v "$PWD/db_writer:/w:ro" -v "$PWD/orchestrator:/o:ro" \
    -v "$PWD/tests/comms:/tests:ro" -w /tmp -e DB_WRITER_DIR=/w -e ORCHESTRATOR_DIR=/o \
    python:3.11-slim \
    sh -c "pip install -q sqlalchemy bcrypt; python /tests/test_geofence.py"
"""

import json
import os
import re
import sys

sys.path.insert(0, os.environ["DB_WRITER_DIR"])

import db_manager as m
from db_manager import (UserDirectory, StreamNotFound, GeofenceNotFound,
                        geofence_to_env)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))


d = UserDirectory.__new__(UserDirectory)
d._engine = create_engine("sqlite:///:memory:")
m.Base.metadata.create_all(d._engine)
Sessions = sessionmaker(bind=d._engine)

alice = d.create_user("a@x.com", "password123")["user_id"]
bob = d.create_user("b@x.com", "password123")["user_id"]

SQUARE = [[11.0, 45.0], [11.1, 45.0], [11.1, 45.1], [11.0, 45.1]]
TRIANGLE = [[9.0, 44.0], [9.5, 44.0], [9.25, 44.5]]

# ── named boundaries ──────────────────────────────────────────────────────────
fence = d.create_geofence(alice, "north pasture", SQUARE)
check("a named boundary is stored", fence["vertices"] == SQUARE, str(fence["vertices"]))
check("...and carries its name", fence["label"] == "north pasture")

listed = d.list_geofences(alice)
check("it appears in the owner's list", len(listed) == 1 and listed[0]["vertices"] == SQUARE)
check("...and not in anybody else's", d.list_geofences(bob) == [])

# ── validation ────────────────────────────────────────────────────────────────
bad = {
    "two points is not a polygon": [[11.0, 45.0], [11.1, 45.0]],
    "no points at all": [],
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
        d.create_geofence(alice, "bad", value)
        check(f"refused: {name}", False)
    except ValueError:
        check(f"refused: {name}", True)

# The one a map UI invites. (lat, lon) is a valid PAIR in the wrong ORDER, and is only
# catchable because latitude is bounded tighter than longitude — a swap inside both
# ranges is undetectable here and always will be.
try:
    d.create_geofence(alice, "swapped", [[45.0, 111.0], [45.1, 111.0], [45.1, 111.1]])
    check("lat/lon swapped is caught when latitude exceeds 90", False)
except ValueError:
    check("lat/lon swapped is caught when latitude exceeds 90", True)

check("a rejected boundary leaves no row", len(d.list_geofences(alice)) == 1)

# ── ownership of the boundary itself ──────────────────────────────────────────
for name, call in {
    "update": lambda: d.update_geofence(fence["geofence_id"], bob, "mine now", TRIANGLE),
    "delete": lambda: d.delete_geofence(fence["geofence_id"], bob),
}.items():
    try:
        call()
        check(f"another tenant cannot {name} a boundary", False)
    except GeofenceNotFound:
        check(f"another tenant cannot {name} a boundary", True)

check("...and the owner's boundary is untouched by those attempts",
      d.list_geofences(alice)[0]["vertices"] == SQUARE)

try:
    d.update_geofence(999999, alice, "x", SQUARE)
    check("a boundary that does not exist is the same refusal", False)
except GeofenceNotFound:
    check("a boundary that does not exist is the same refusal", True)

# ── assigning one to a slot ───────────────────────────────────────────────────
s_fenced = d.create_stream(alice, "north field", None, fence["geofence_id"])
check("a slot may be created pointing at a boundary",
      s_fenced["geofence_id"] == fence["geofence_id"])

s_plain = d.create_stream(alice, "unfenced")
check("...or with none at all", s_plain["geofence_id"] is None)

d.set_stream_geofence(s_plain["stream_id"], alice, fence["geofence_id"])
by_id = {s["stream_id"]: s for s in d.list_streams(alice)}
check("a boundary can be assigned afterwards",
      by_id[s_plain["stream_id"]]["geofence_id"] == fence["geofence_id"])

d.set_stream_geofence(s_plain["stream_id"], alice, None)
check("...and unassigned, which disables geofencing",
      {s["stream_id"]: s for s in d.list_streams(alice)}[s_plain["stream_id"]]["geofence_id"] is None)

# Bob's boundary must not be reachable from Alice's slot, and vice versa. This pairing
# is the one that matters: both ids are sequential across every tenant.
bobs_fence = d.create_geofence(bob, "bob's field", TRIANGLE)
try:
    d.set_stream_geofence(s_fenced["stream_id"], alice, bobs_fence["geofence_id"])
    check("a slot cannot point at another tenant's boundary", False)
except GeofenceNotFound:
    check("a slot cannot point at another tenant's boundary", True)

try:
    d.create_stream(alice, "sneaky", None, bobs_fence["geofence_id"])
    check("...not at creation time either", False)
except GeofenceNotFound:
    check("...not at creation time either", True)

try:
    d.set_stream_geofence(s_fenced["stream_id"], bob, bobs_fence["geofence_id"])
    check("another tenant cannot assign anything to this slot", False)
except StreamNotFound:
    check("another tenant cannot assign anything to this slot", True)

check("...and the slot still points where its owner left it",
      {s["stream_id"]: s for s in d.list_streams(alice)}[s_fenced["stream_id"]]["geofence_id"]
      == fence["geofence_id"])

# ── rendering into the app's spelling ─────────────────────────────────────────
rendered = geofence_to_env(json.dumps(SQUARE))
check("rendered in the form the app's parser reads",
      rendered == "(11.0, 45.0), (11.1, 45.0), (11.1, 45.1), (11.0, 45.1)", rendered)
check("no fence renders as nothing, not an empty string", geofence_to_env(None) is None)

# The app's own regex, reproduced rather than imported because the app tree is not on
# this path. Two services hold two spellings of one value; this is the only place they
# are compared.
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

opened_none = d.open_flight_for_key(s_plain["stream_key"])
opened_none["publisher_token"] = "tok"
env_none = build_flight_env(opened_none, {"DB_WRITER_URL": "x"}, s_plain["stream_key"])
check("a slot with no fence injects nothing at all", "GEOFENCING_VERTEXES" not in env_none)

# The control for the line above: an empty string is NOT the same as an absent
# variable. env_ignore_empty makes the app treat them alike today, and that is a
# setting somebody could change; absent is unambiguous.
check("...and specifically not an empty string",
      env_none.get("GEOFENCING_VERTEXES", None) is None)

# Two tenants, two fences, no bleed — the defect this whole change exists to fix.
b_stream = d.create_stream(bob, "bob's slot", None, bobs_fence["geofence_id"])
b_opened = d.open_flight_for_key(b_stream["stream_key"])
check("two tenants' flights carry two different fences",
      b_opened["geofence_vertexes"] != opened["geofence_vertexes"],
      f"{b_opened['geofence_vertexes']} vs {opened['geofence_vertexes']}")

# ── the snapshot ──────────────────────────────────────────────────────────────
#
# This is what makes a NAMED boundary safe to edit or delete. Without it, editing a
# fence silently restates what every past flight was judged against, and "which
# boundary produced this alert?" — the question a tenant disputing one asks — gets
# answered with today's shape rather than the one that flew.

MOVED = [[20.0, 50.0], [20.1, 50.0], [20.1, 50.1]]
d.update_geofence(fence["geofence_id"], alice, "north pasture, resurveyed", MOVED)

check("editing a boundary changes what the NEXT flight is given",
      d.open_flight_for_key(s_fenced["stream_key"])["geofence_vertexes"]
      == geofence_to_env(json.dumps(MOVED)))

with Sessions() as session:
    flown = session.query(m.Flight).filter_by(flight_id=opened["flight_id"]).first()
    check("...and does NOT change the flight already recorded",
          json.loads(flown.geofence) == SQUARE, str(json.loads(flown.geofence)))

# Deleting is the harder half: a foreign key would have to refuse it or null it, and
# either way the past flight loses the answer.
d.delete_geofence(fence["geofence_id"], alice)
check("a deleted boundary is gone from the list", d.list_geofences(alice) == [])
check("...and the slots using it stop geofencing rather than dangling",
      all(s["geofence_id"] is None for s in d.list_streams(alice)))

with Sessions() as session:
    flown = session.query(m.Flight).filter_by(flight_id=opened["flight_id"]).first()
    check("...while the flight it judged still records the boundary it used",
          json.loads(flown.geofence) == SQUARE)

check("a slot whose boundary was deleted injects nothing",
      "GEOFENCING_VERTEXES" not in build_flight_env(
          dict(d.open_flight_for_key(s_fenced["stream_key"]), publisher_token="t"),
          {"DB_WRITER_URL": "x"}, s_fenced["stream_key"]))

check("deleting alice's boundary left bob's alone", len(d.list_geofences(bob)) == 1)

print()
print("=" * 60)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
