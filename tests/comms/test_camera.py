"""
Named camera profiles, slot assignment, and the snapshot that keeps history honest.

The optics a flight measures ground distances with were five module constants in
app/shared/processes/constants.py describing ONE airframe — a Mavic 3 Enterprise —
forwarded to every flight container on the deployment. So a deployment served exactly
one kind of camera, and a tenant flying anything else got measurements scaled by the
ratio between their sensor and somebody else's.

Four groups:

  validation   positivity, whole pixels, and the cross-field rule that matters: the
               millimetre and pixel aspect ratios must agree. A mismatch does not
               fail, it silently stretches every ground measurement on one axis.
  ownership    two sequential ids — stream_id and drone_id — and a guess at either
               must learn nothing and change nothing.
  snapshot     what makes a named profile safe to correct or delete: a flight records
               the optics it MEASURED WITH, not a pointer to today's numbers.
  injection    the profile is rendered into the five variables AppSettings reads, and
               no profile injects nothing rather than empty ones.

Run with no stack:

  docker run --rm -v "$PWD/db_writer:/w:ro" -v "$PWD/orchestrator:/o:ro" \
    -v "$PWD/tests/comms:/tests:ro" -w /tmp -e DB_WRITER_DIR=/w -e ORCHESTRATOR_DIR=/o \
    python:3.11-slim \
    sh -c "pip install -q sqlalchemy bcrypt; python /tests/test_camera.py"
"""

import json
import os
import sys

sys.path.insert(0, os.environ["DB_WRITER_DIR"])

import db_manager as m
from db_manager import (UserDirectory, StreamNotFound, DroneNotFound, camera_to_env)
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

# The 4/3 CMOS the constants describe: 17.35/13.00 = 1.3346, 5280/3956 = 1.3347.
MAVIC = {"focal_len_mm": 12.29, "sensor_width_mm": 17.35, "sensor_height_mm": 13.00,
         "sensor_width_px": 5280, "sensor_height_px": 3956}
# A 1" sensor, 3:2 both ways.
OTHER = {"focal_len_mm": 8.8, "sensor_width_mm": 13.2, "sensor_height_mm": 8.8,
         "sensor_width_px": 5472, "sensor_height_px": 3648}

# ── named profiles ────────────────────────────────────────────────────────────
cam = d.create_drone(alice, "Mavic 3 Enterprise", MAVIC)
check("a profile is stored", cam["focal_len_mm"] == 12.29 and cam["sensor_width_px"] == 5280)
check("...and carries its name", cam["label"] == "Mavic 3 Enterprise")
check("it appears in the owner's list and nobody else's",
      len(d.list_drones(alice)) == 1 and d.list_drones(bob) == [])

# ── validation ────────────────────────────────────────────────────────────────
bad = {
    "a missing value": {**MAVIC, "focal_len_mm": None},
    "an empty value": {**MAVIC, "sensor_width_mm": ""},
    "zero focal length": {**MAVIC, "focal_len_mm": 0},
    "negative focal length": {**MAVIC, "focal_len_mm": -12.29},
    "negative pixels": {**MAVIC, "sensor_width_px": -5280},
    "not a number": {**MAVIC, "sensor_height_mm": "thirteen"},
    "fractional pixels": {**MAVIC, "sensor_width_px": 5280.5},
    "not a mapping at all": [12.29, 17.35, 13.0, 5280, 3956],
}
for name, value in bad.items():
    try:
        d.create_drone(alice, "bad", value)
        check(f"refused: {name}", False)
    except ValueError:
        check(f"refused: {name}", True)

# The cross-field rule, and the reason it is worth having. Every value below is
# individually valid and positive; only their RATIOS disagree — 4:3 in millimetres
# against 3:2 in pixels. Nothing downstream would raise; ground sampling distance
# would simply come out stretched on one axis.
try:
    d.create_drone(alice, "mismatched", {**MAVIC, "sensor_width_px": 5472,
                                         "sensor_height_px": 3648})
    check("refused: sensor aspect ratios that disagree", False)
except ValueError as e:
    check("refused: sensor aspect ratios that disagree", "aspect ratio" in str(e).lower(),
          str(e)[:70])

check("...while a sensor whose ratios agree is accepted",
      d.create_drone(alice, "1 inch", OTHER)["sensor_width_px"] == 5472)
check("a rejected profile leaves no row", len(d.list_drones(alice)) == 2)

# ── ownership ─────────────────────────────────────────────────────────────────
for name, call in {
    "update": lambda: d.update_drone(cam["drone_id"], bob, "mine", OTHER),
    "delete": lambda: d.delete_drone(cam["drone_id"], bob),
}.items():
    try:
        call()
        check(f"another tenant cannot {name} a profile", False)
    except DroneNotFound:
        check(f"another tenant cannot {name} a profile", True)

check("...and the owner's numbers are untouched by those attempts",
      d.list_drones(alice)[0]["focal_len_mm"] == 12.29)

try:
    d.update_drone(999999, alice, "x", MAVIC)
    check("a profile that does not exist is the same refusal", False)
except DroneNotFound:
    check("a profile that does not exist is the same refusal", True)

# ── assigning one to a slot ───────────────────────────────────────────────────
s_cam = d.create_stream(alice, "north field", None, None, cam["drone_id"])
check("a slot may be created pointing at a profile", s_cam["drone_id"] == cam["drone_id"])

s_plain = d.create_stream(alice, "default optics")
check("...or with none, using the deployment's", s_plain["drone_id"] is None)

d.set_stream_drone(s_plain["stream_id"], alice, cam["drone_id"])
check("a profile can be assigned afterwards",
      {s["stream_id"]: s for s in d.list_streams(alice)}[s_plain["stream_id"]]["drone_id"]
      == cam["drone_id"])
d.set_stream_drone(s_plain["stream_id"], alice, None)
check("...and unassigned back to the deployment's",
      {s["stream_id"]: s for s in d.list_streams(alice)}[s_plain["stream_id"]]["drone_id"] is None)

bobs_cam = d.create_drone(bob, "bob's camera", OTHER)
try:
    d.set_stream_drone(s_cam["stream_id"], alice, bobs_cam["drone_id"])
    check("a slot cannot point at another tenant's profile", False)
except DroneNotFound:
    check("a slot cannot point at another tenant's profile", True)

try:
    d.create_stream(alice, "sneaky", None, None, bobs_cam["drone_id"])
    check("...not at creation time either", False)
except DroneNotFound:
    check("...not at creation time either", True)

try:
    d.set_stream_drone(s_cam["stream_id"], bob, bobs_cam["drone_id"])
    check("another tenant cannot assign anything to this slot", False)
except StreamNotFound:
    check("another tenant cannot assign anything to this slot", True)

check("...and the slot still points where its owner left it",
      {s["stream_id"]: s for s in d.list_streams(alice)}[s_cam["stream_id"]]["drone_id"]
      == cam["drone_id"])

# ── rendering into the app's variables ────────────────────────────────────────
env_vars = camera_to_env(json.dumps({k: v for k, v in MAVIC.items()}))
check("rendered as the five variables AppSettings reads",
      set(env_vars) == {"DRONE_TRUE_FOCAL_LEN_MM", "DRONE_SENSOR_WIDTH_MM",
                        "DRONE_SENSOR_HEIGHT_MM", "DRONE_SENSOR_WIDTH_PIXELS",
                        "DRONE_SENSOR_HEIGHT_PIXELS"},
      str(sorted(env_vars)))
check("...with the values, as strings an environment can carry",
      env_vars["DRONE_TRUE_FOCAL_LEN_MM"] == "12.29"
      and env_vars["DRONE_SENSOR_WIDTH_PIXELS"] == "5280")
check("no profile renders as no variables at all", camera_to_env(None) == {})

# ── injection ─────────────────────────────────────────────────────────────────
sys.path.insert(0, os.environ["ORCHESTRATOR_DIR"])
from flights import build_flight_env

BASE = {"DB_WRITER_URL": "x", "DRONE_TRUE_FOCAL_LEN_MM": "99.9"}

opened = d.open_flight_for_key(s_cam["stream_key"])
opened["publisher_token"] = "tok"
env = build_flight_env(opened, BASE, s_cam["stream_key"])
check("the slot's optics beat the deployment's",
      env["DRONE_TRUE_FOCAL_LEN_MM"] == "12.29", env["DRONE_TRUE_FOCAL_LEN_MM"])
check("...and all five arrive together",
      env["DRONE_SENSOR_HEIGHT_PIXELS"] == "3956" and env["DRONE_SENSOR_WIDTH_MM"] == "17.35")

opened_none = d.open_flight_for_key(s_plain["stream_key"])
opened_none["publisher_token"] = "tok"
env_none = build_flight_env(opened_none, BASE, s_plain["stream_key"])
# The assertion protecting every deployment that exists today: a slot naming no
# profile must leave the configured optics exactly as they were.
check("no profile leaves the deployment's optics standing",
      env_none["DRONE_TRUE_FOCAL_LEN_MM"] == "99.9", env_none["DRONE_TRUE_FOCAL_LEN_MM"])
check("...and injects none of the other four",
      "DRONE_SENSOR_WIDTH_MM" not in env_none)

b_stream = d.create_stream(bob, "bob's slot", None, None, bobs_cam["drone_id"])
b_env = build_flight_env(
    dict(d.open_flight_for_key(b_stream["stream_key"]), publisher_token="t"),
    BASE, b_stream["stream_key"])
check("two tenants' flights are measured with two different cameras",
      b_env["DRONE_TRUE_FOCAL_LEN_MM"] != env["DRONE_TRUE_FOCAL_LEN_MM"],
      f"{b_env['DRONE_TRUE_FOCAL_LEN_MM']} vs {env['DRONE_TRUE_FOCAL_LEN_MM']}")

# ── the snapshot ──────────────────────────────────────────────────────────────
#
# The reason a profile is safe to correct at all. A focal length typed wrong and fixed
# a month later must not restate what every past flight measured with — an alert has
# to stay explicable by the numbers that actually produced it.

d.update_drone(cam["drone_id"], alice, "Mavic 3E, recalibrated",
               {**MAVIC, "focal_len_mm": 12.50})

check("correcting a profile changes what the NEXT flight measures with",
      d.open_flight_for_key(s_cam["stream_key"])["camera_env"]["DRONE_TRUE_FOCAL_LEN_MM"]
      == "12.5")

with Sessions() as session:
    flown = session.query(m.Flight).filter_by(flight_id=opened["flight_id"]).first()
    check("...and does NOT change the flight already recorded",
          json.loads(flown.camera)["focal_len_mm"] == 12.29,
          str(json.loads(flown.camera)["focal_len_mm"]))

# Deleting is what "I sold that drone" means, and it has to be a hard delete without
# taking the history with it.
d.delete_drone(cam["drone_id"], alice)
check("a deleted profile is gone from the list",
      cam["drone_id"] not in [x["drone_id"] for x in d.list_drones(alice)])
check("...and the slots using it fall back rather than dangling",
      all(s["drone_id"] is None for s in d.list_streams(alice)))

with Sessions() as session:
    flown = session.query(m.Flight).filter_by(flight_id=opened["flight_id"]).first()
    check("...while the flight it measured still records the optics it used",
          json.loads(flown.camera)["focal_len_mm"] == 12.29)

check("a slot whose profile was deleted uses the deployment's again",
      build_flight_env(
          dict(d.open_flight_for_key(s_cam["stream_key"]), publisher_token="t"),
          BASE, s_cam["stream_key"])["DRONE_TRUE_FOCAL_LEN_MM"] == "99.9")

check("deleting alice's profile left bob's alone", len(d.list_drones(bob)) == 1)

print()
print("=" * 60)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
