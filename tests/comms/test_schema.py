"""
Schema and stream-management behaviour, against SQLite in memory.

Needs no running stack — this is the one file here that is pure logic. It pins the
three properties the portal depends on:

  add     -> a new, unique stream key
  rotate  -> the key changes and NO flight or alert is touched
  remove  -> revoked and hidden, NOTHING deleted

plus the ownership rules: a flight's owner is reached through its stream (there is no
redundant user_id to contradict it), and no user can touch another user's streams.

Run:  see README.md
"""
import os
import sys

# db_writer is not a package on the path; point at it explicitly so this file can be
# run from anywhere. DB_WRITER_DIR overrides for a container mount.
sys.path.insert(0, os.environ.get(
    "DB_WRITER_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "db_writer")))

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import db_manager as m

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def build():
    """A UserDirectory bound to a throwaway in-memory database."""
    d = m.UserDirectory.__new__(m.UserDirectory)      # skip __init__; no real DB URL
    d._engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(d._engine)
    return d, sessionmaker(bind=d._engine)


def main():
    d, S = build()

    # ── shape ────────────────────────────────────────────────────────────────
    insp = inspect(d._engine)
    check("tables are users/streams/flights/alerts/recordings",
          sorted(insp.get_table_names()) == ["alerts", "flights", "recordings", "streams", "users"],
          str(sorted(insp.get_table_names())))
    flight_cols = [c["name"] for c in insp.get_columns("flights")]
    check("flights carries NO user_id (linear ownership)", "user_id" not in flight_cols,
          str(flight_cols))
    check("flights carries stream_id + public_uuid + output_path",
          {"stream_id", "public_uuid", "output_path"} <= set(flight_cols))

    with S() as s:
        s.add(m.User(email="a@b.c", password=m.User.hash_password("pw")))
        s.add(m.User(email="x@y.z", password=m.User.hash_password("pw")))
        s.commit()
    u1 = d.authenticate("a@b.c", "pw")
    u2 = d.authenticate("x@y.z", "pw")

    # ── key generation ───────────────────────────────────────────────────────
    keys = {m.generate_stream_key() for _ in range(5000)}
    check("5000 generated keys, no collisions", len(keys) == 5000, f"unique={len(keys)}")
    check("keys use only the unambiguous alphabet",
          all(set(k) <= set(m.STREAM_KEY_ALPHABET) for k in keys))
    check("alphabet excludes i/l/o/u", not (set(m.STREAM_KEY_ALPHABET) & set("ilou")))

    # ── 1. ADD ───────────────────────────────────────────────────────────────
    a = d.create_stream(u1, "feed-1")
    b = d.create_stream(u1, "feed-2")
    c = d.create_stream(u1, "feed-3")
    check("adding streams yields distinct keys",
          len({a["stream_key"], b["stream_key"], c["stream_key"]}) == 3)
    check("a new key resolves to its owner",
          d.resolve_stream_key(a["stream_key"])["user_id"] == u1)
    check("an unknown key resolves to nothing", d.resolve_stream_key("nope") is None)

    with S() as s:
        for st in (a, b, c):
            f = m.Flight(stream_id=st["stream_id"])
            s.add(f)
            s.commit()
            s.add(m.Alert(flight_id=f.flight_id, alert_msg="history"))
            s.commit()
        base_flights = s.query(m.Flight).count()
        base_alerts = s.query(m.Alert).count()

    # ownership resolves through the stream
    with S() as s:
        f = s.query(m.Flight).first()
        check("flight.user_id resolves via its stream", f.user_id == u1, f"got={f.user_id}")
        first_flight_id, first_public_uuid = f.flight_id, f.public_uuid

    # ── recordings ───────────────────────────────────────────────────────────
    with S() as s:
        base_recordings = s.query(m.Recording).count()

    check("an unknown output uuid resolves to nothing",
          d.record_upload("00000000-0000-0000-0000-000000000000", "/x", "local") is None)
    with S() as s:
        check("an unknown uuid added no recording row",
              s.query(m.Recording).count() == base_recordings)

    got_flight_id = d.record_upload(first_public_uuid, "/recordings/out/x/seg.mp4", "local")
    check("a known output uuid resolves to its flight", got_flight_id == first_flight_id)
    with S() as s:
        rec = s.query(m.Recording).filter_by(flight_id=first_flight_id).first()
        check("the recording is stored against that flight", rec is not None)
        check("storage_location is None for the local backend",
              rec.storage_location is None, str(rec.storage_location))

    # ── 2. ROTATE ────────────────────────────────────────────────────────────
    old = b["stream_key"]
    new = d.rotate_stream_key(b["stream_id"], u1)
    check("rotation changes the key", old != new)
    check("old key is dead immediately", d.resolve_stream_key(old) is None)
    check("new key is live", d.resolve_stream_key(new) is not None)
    with S() as s:
        check("rotation touches no flight", s.query(m.Flight).count() == base_flights)
        check("rotation touches no alert", s.query(m.Alert).count() == base_alerts)
        check("flight still maps to the same stream",
              s.query(m.Flight).filter_by(stream_id=b["stream_id"]).count() == 1)

    # ── 3. REMOVE ────────────────────────────────────────────────────────────
    d.revoke_stream(c["stream_id"], u1)
    check("retired key is unusable", d.resolve_stream_key(c["stream_key"]) is None)
    check("sibling streams unaffected", d.resolve_stream_key(a["stream_key"]) is not None)
    check("portal view hides retired slots",
          [x["label"] for x in d.list_streams(u1)] == ["feed-1", "feed-2"])
    check("history view still shows it",
          len(d.list_streams(u1, include_revoked=True)) == 3)
    with S() as s:
        check("removal deletes NO flight", s.query(m.Flight).count() == base_flights)
        check("removal deletes NO alert", s.query(m.Alert).count() == base_alerts)

    check("rotation revives a retired slot",
          d.resolve_stream_key(d.rotate_stream_key(c["stream_id"], u1)) is not None)

    # ── cross-user isolation ─────────────────────────────────────────────────
    def refused(fn, *args):
        try:
            fn(*args)
            return False
        except ValueError:
            return True

    check("cannot revoke another user's stream", refused(d.revoke_stream, a["stream_id"], u2))
    check("cannot rotate another user's stream", refused(d.rotate_stream_key, a["stream_id"], u2))
    check("list_streams is per-user", d.list_streams(u2) == [])
    check("latest_flight_id is per-user", d.latest_flight_id(u2) is None)
    check("latest_flight_id finds own flight", d.latest_flight_id(u1) is not None)

    # ── constraints and cascade ──────────────────────────────────────────────
    with S() as s:
        try:
            s.add(m.Flight())
            s.commit()
            check("flight without stream_id rejected", False, "it was ACCEPTED")
        except Exception:
            s.rollback()
            check("flight without stream_id rejected", True)

    # Deleting a USER must erase everything below it (account erasure), which is the
    # only reason the streams -> flights cascade exists.
    with S() as s:
        s.delete(s.get(m.User, u1))
        s.commit()
        check("deleting a user cascades to streams/flights/alerts/recordings",
              s.query(m.Stream).count() == 0 and s.query(m.Flight).count() == 0
              and s.query(m.Alert).count() == 0 and s.query(m.Recording).count() == 0)

    check("there is deliberately no delete_stream()", not hasattr(d, "delete_stream"))

    print("\n" + "=" * 60)
    print(f"{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


sys.exit(main())
