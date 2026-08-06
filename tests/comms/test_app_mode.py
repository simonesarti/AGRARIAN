"""
Per-slot processing mode, against SQLite in memory and a fake flight open.

APP_MODE used to be deployment-wide — one variable on the orchestrator, forwarded to
every flight container it ever started — so one cluster served exactly one product and
a livestock tenant and a terrain tenant could not share it. It now lives on the stream
slot, and this pins the whole path from the column to the container's environment.

Three groups, and the last is the one that matters:

  validation   an unrecognised mode must be refused HERE. It becomes an environment
               variable inside a GPU container, where a bad value is a process that
               exits at startup — which surfaces as a drone publishing into nothing,
               not as an error anyone sees.
  ownership    setting a mode is a stream operation like any other, so another
               tenant's stream_id must answer exactly as an absent one does, and must
               leave the owner's value alone. StreamNotFound rather than a message
               match, so the HTTP layer can tell 404 from 400 by type (§3's rule).
  injection    the slot's mode must beat base_env, and NO preference must leave
               base_env standing — that second one is what keeps a single-product
               deployment behaving exactly as it did before this column existed.

Run it with no stack at all:

  docker run --rm -v "$PWD/db_writer:/w:ro" -v "$PWD/orchestrator:/o:ro" \
    -v "$PWD/tests/comms:/tests:ro" -w /tmp -e DB_WRITER_DIR=/w -e ORCHESTRATOR_DIR=/o \
    python:3.11-slim sh -c "pip install -q sqlalchemy bcrypt; python /tests/test_app_mode.py"
"""

import os, sys
sys.path.insert(0, os.environ["DB_WRITER_DIR"])
os.environ["DB_URL"] = "sqlite://"
import db_manager as m
from db_manager import UserDirectory, StreamNotFound
from sqlalchemy import create_engine

r = []
def check(name, ok, extra=""):
    r.append(ok); print(("PASS  " if ok else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))

# Same construction as test_schema.py: skip __init__ so no pooled engine is built.
d = UserDirectory.__new__(UserDirectory)
d._engine = create_engine("sqlite:///:memory:")
m.Base.metadata.create_all(d._engine)
alice = d.create_user("a@x.com", "password123")["user_id"]
bob   = d.create_user("b@x.com", "password123")["user_id"]

# --- creation ---
s_default = d.create_stream(alice, "no preference")
check("a slot may express no preference", s_default["app_mode"] is None, str(s_default["app_mode"]))

s_dd = d.create_stream(alice, "terrain", "danger_detection")
check("a slot may name danger_detection", s_dd["app_mode"] == "danger_detection")

s_hm = d.create_stream(alice, "herd", "health_monitoring")
check("a slot may name health_monitoring", s_hm["app_mode"] == "health_monitoring")

check("mode is normalised", d.create_stream(alice, "x", "  Danger_Detection  ")["app_mode"] == "danger_detection")
check("empty string is no preference", d.create_stream(alice, "y", "   ")["app_mode"] is None)

for bad in ("herd_monitoring", "danger", "DROP TABLE streams", "danger_detection;x"):
    try:
        d.create_stream(alice, "bad", bad); check(f"unknown mode {bad!r} refused", False)
    except ValueError as e:
        check(f"unknown mode {bad!r} refused", "Unknown processing mode" in str(e))

# --- listing ---
listed = {s["stream_id"]: s for s in d.list_streams(alice)}
check("list_streams reports the mode", listed[s_hm["stream_id"]]["app_mode"] == "health_monitoring")
check("...and None for a slot without one", listed[s_default["stream_id"]]["app_mode"] is None)

# --- changing ---
d.set_stream_mode(s_default["stream_id"], alice, "health_monitoring")
check("mode can be set on an existing slot",
      {s["stream_id"]: s for s in d.list_streams(alice)}[s_default["stream_id"]]["app_mode"] == "health_monitoring")

d.set_stream_mode(s_default["stream_id"], alice, None)
check("...and cleared back to the deployment default",
      {s["stream_id"]: s for s in d.list_streams(alice)}[s_default["stream_id"]]["app_mode"] is None)

try:
    d.set_stream_mode(s_dd["stream_id"], bob, "health_monitoring"); check("another tenant cannot set the mode", False)
except StreamNotFound:
    check("another tenant cannot set the mode", True)
check("...and the owner's mode is untouched by that attempt",
      {s["stream_id"]: s for s in d.list_streams(alice)}[s_dd["stream_id"]]["app_mode"] == "danger_detection")

try:
    d.set_stream_mode(999999, alice, "danger_detection"); check("a stream that does not exist is the same refusal", False)
except StreamNotFound:
    check("a stream that does not exist is the same refusal", True)

try:
    d.set_stream_mode(s_dd["stream_id"], alice, "nonsense"); check("a bad mode on an owned slot is NOT StreamNotFound", False)
except StreamNotFound:
    check("a bad mode on an owned slot is NOT StreamNotFound", False, "raised StreamNotFound — HTTP would 404 a 400")
except ValueError:
    check("a bad mode on an owned slot is NOT StreamNotFound", True)

# --- the whole point: it reaches the container env ---
opened = d.open_flight_for_key(s_hm["stream_key"])
check("open_flight_for_key carries the mode", opened["app_mode"] == "health_monitoring", str(opened["app_mode"]))
opened_none = d.open_flight_for_key(s_default["stream_key"])
check("...and None when the slot has no preference", opened_none["app_mode"] is None)

sys.path.insert(0, os.environ["ORCHESTRATOR_DIR"])
from flights import build_flight_env
base = {"APP_MODE": "danger_detection", "DB_WRITER_URL": "http://db-writer:8000"}
opened["publisher_token"] = "tok"
env = build_flight_env(opened, base, s_hm["stream_key"])
check("the slot's mode wins over the deployment's", env["APP_MODE"] == "health_monitoring", env["APP_MODE"])

opened_none["publisher_token"] = "tok"
env2 = build_flight_env(opened_none, base, s_default["stream_key"])
check("no preference leaves the deployment's standing", env2["APP_MODE"] == "danger_detection", env2["APP_MODE"])

env3 = build_flight_env(opened_none, {"DB_WRITER_URL": "x"}, s_default["stream_key"])
check("...and injects nothing at all when there is none either side", "APP_MODE" not in env3)

check("no end-user credential ever appears", not any(k in env for k in ("DB_USERNAME", "DB_PASSWORD", "EMAIL", "PASSWORD")))

print()
print("=" * 60)
print(f"{sum(r)}/{len(r)} passed")
sys.exit(0 if all(r) else 1)
