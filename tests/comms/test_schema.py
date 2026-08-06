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
from datetime import datetime, timezone

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


def raises(exc, fn, *args):
    """True if fn(*args) raises exc. Anything else propagates — a test that passes
    because the wrong error was raised is worse than one that fails."""
    try:
        fn(*args)
    except exc:
        return True
    return False


def stored_password(SessionFactory, user_id):
    with SessionFactory() as s:
        return s.query(m.User).filter_by(user_id=user_id).first().password


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
    # An exact list, not a subset. A table appearing here that nobody added on purpose
    # is worth failing over — this assertion is what caught `geofences` arriving.
    check("tables are users/streams/geofences/drones/flights/alerts/recordings",
          sorted(insp.get_table_names()) ==
          ["alerts", "drones", "flights", "geofences", "recordings", "streams", "users"],
          str(sorted(insp.get_table_names())))
    # A camera PROFILE, not an aircraft. §5's "nothing in the schema models a physical
    # drone" survives because nothing here identifies one: no serial, no registration,
    # nothing unique. Two users flying the same model hold two unrelated rows.
    drone_cols = [c["name"] for c in insp.get_columns("drones")]
    check("drones carry optics and no aircraft identity",
          "focal_len_mm" in drone_cols
          and not any(c in drone_cols for c in ("serial", "serial_number", "registration")),
          str(drone_cols))
    # Configuration, and therefore NOT a credential: a geofence is reached only
    # through its owner, exactly as a stream is.
    check("geofences hang off a user",
          "user_id" in [c["name"] for c in insp.get_columns("geofences")])
    # The snapshot that makes a named boundary safe to edit or delete. Without this
    # column, history would point at whatever the polygon looks like today.
    check("flights record the boundary they were judged against",
          "geofence" in [c["name"] for c in insp.get_columns("flights")])
    check("...and the optics they were measured with",
          "camera" in [c["name"] for c in insp.get_columns("flights")])
    flight_cols = [c["name"] for c in insp.get_columns("flights")]
    check("flights carries NO user_id (linear ownership)", "user_id" not in flight_cols,
          str(flight_cols))
    check("flights carries stream_id + public_uuid + output_path",
          {"stream_id", "public_uuid", "output_path"} <= set(flight_cols))

    # ── registration ─────────────────────────────────────────────────────────
    # Open to anyone (§3), so every argument here is untrusted input.
    created = d.create_user("a@b.c", "correct horse")
    d.create_user("x@y.z", "correct horse")
    check("create_user returns the new user_id", isinstance(created["user_id"], int))
    u1 = d.authenticate("a@b.c", "correct horse")
    u2 = d.authenticate("x@y.z", "correct horse")
    check("a registered user authenticates", u1 == created["user_id"])
    check("the wrong password is refused", raises(ValueError, d.authenticate, "a@b.c", "nope"))

    check("the password is not stored in plaintext", stored_password(S, u1) != "correct horse")

    # Case and whitespace: the unique constraint is case-sensitive in PostgreSQL, so
    # without normalisation these are two accounts and the second login silently fails.
    check("a duplicate email is refused",
          raises(m.EmailAlreadyRegistered, d.create_user, "a@b.c", "correct horse"))
    check("the same email in another case is a duplicate too",
          raises(m.EmailAlreadyRegistered, d.create_user, "A@B.C", "correct horse"))
    check("surrounding whitespace does not make a new account",
          raises(m.EmailAlreadyRegistered, d.create_user, "  a@b.c  ", "correct horse"))
    check("login works in any casing", d.authenticate("A@b.C", "correct horse") == u1)
    check("EmailAlreadyRegistered is still a ValueError",
          issubclass(m.EmailAlreadyRegistered, ValueError))

    check("a too-short password is refused",
          raises(ValueError, d.create_user, "new@b.c", "a" * (m.MIN_PASSWORD_LENGTH - 1)))
    # bcrypt ignores everything past the 72nd byte and 5.x raises rather than
    # truncating, so this has to be caught before hashing or it is a 500.
    check("a password over bcrypt's 72-byte limit is refused",
          raises(ValueError, d.create_user, "new@b.c", "a" * (m.MAX_PASSWORD_BYTES + 1)))
    check("the byte limit counts bytes, not characters",
          raises(ValueError, d.create_user, "new@b.c", "🔒" * 19))    # 76 bytes, 19 chars
    check("a 72-byte password is accepted",
          d.create_user("edge@b.c", "a" * m.MAX_PASSWORD_BYTES)["user_id"] > 0)

    for bad in ("", "   ", "nope", "@b.c", "a@", "a@b", "a b@c.d"):
        check(f"a malformed email is refused: {bad!r}",
              raises(ValueError, d.create_user, bad, "correct horse"))
    # 254 is RFC 5321's limit and therefore legal; 255 is not.
    check("an email at exactly the length limit is accepted",
          d.create_user("a" * 250 + "@b.c", "correct horse")["user_id"] > 0)
    check("an over-long email is refused",
          raises(ValueError, d.create_user, "a" * 251 + "@b.c", "correct horse"))

    # A rejected registration must leave nothing behind — a half-created account
    # would take the address without being loginable.
    check("a rejected registration creates no row",
          raises(ValueError, d.authenticate, "new@b.c", "correct horse"))

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

    # ── active_flights (viewer-token disambiguation) ─────────────────────────
    check("active_flights is per-user", d.active_flights(u2) == [])
    check("u1's three still-open flights (created above) are all active",
          len(d.active_flights(u1)) == 3)

    with S() as s:
        fresh = m.User(email="solo@test.io", password=m.User.hash_password("pw"))
        s.add(fresh)
        s.commit()
        solo = fresh.user_id
    solo_stream = d.create_stream(solo, "solo-feed")
    with S() as s:
        f = m.Flight(stream_id=solo_stream["stream_id"])
        s.add(f)
        s.commit()
        solo_flight_id = f.flight_id

    check("exactly one active flight resolves unambiguously — nothing to ask",
          [x["flight_id"] for x in d.active_flights(solo)] == [solo_flight_id])
    check("active_flights carries the stream_id needed to disambiguate",
          d.active_flights(solo)[0]["stream_id"] == solo_stream["stream_id"])

    with S() as s:
        s.get(m.Flight, solo_flight_id).end_time = datetime.now(timezone.utc)
        s.commit()
    check("a landed flight (end_time set) is NOT active — 'latest' is not 'active'",
          d.active_flights(solo) == [])

    # This user only exists to isolate the assertions above from u1's incidental
    # flights; remove it so the cascade-delete check below still sees a clean slate.
    with S() as s:
        s.delete(s.get(m.User, solo))
        s.commit()

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

    # ── the per-user slot cap ────────────────────────────────────────────────
    # A slot is what lets a GPU container exist and registration is open, so this
    # is the only thing between an anonymous signup and unbounded GPU spend.
    d2, S2 = build()
    cap = m._MAX_STREAMS_PER_USER
    capped = d2.create_user("capped@b.c", "correct horse")["user_id"]
    slots = [d2.create_stream(capped, f"slot-{i}") for i in range(cap)]
    check(f"a user may hold {cap} active slots", len(d2.list_streams(capped)) == cap)
    check("the next one is refused",
          raises(m.StreamLimitReached, d2.create_stream, capped, "one too many"))
    check("StreamLimitReached is a ValueError",
          issubclass(m.StreamLimitReached, ValueError))

    # Retiring frees a slot, because a retired one cannot publish.
    d2.revoke_stream(slots[0]["stream_id"], capped)
    check("retiring one frees a slot", d2.create_stream(capped, "replacement")["stream_id"] > 0)

    # THE BYPASS: rotation revives a retired slot, so revoke -> add -> rotate would
    # net one slot over the cap on every repeat if rotation were not capped too.
    check("reviving a retired slot by rotation is refused at the cap",
          raises(m.StreamLimitReached, d2.rotate_stream_key, slots[0]["stream_id"], capped))
    check("the bypass really would have exceeded the cap",
          len(d2.list_streams(capped)) == cap)

    # ...but rotating an ACTIVE slot at the cap is fine: it revives nothing.
    before = d2.list_streams(capped)[0]["stream_key"]
    after = d2.rotate_stream_key(slots[1]["stream_id"], capped)
    check("rotating an active slot at the cap still works", after != before)
    check("rotation did not change the number of active slots",
          len(d2.list_streams(capped)) == cap)

    # Below the cap, reviving is exactly how a user brings a slot back.
    d2.revoke_stream(slots[2]["stream_id"], capped)
    revived = d2.rotate_stream_key(slots[0]["stream_id"], capped)
    check("below the cap, rotation revives a retired slot", len(revived) == m.STREAM_KEY_LENGTH)
    check("the revived slot is active again",
          slots[0]["stream_id"] in [s["stream_id"] for s in d2.list_streams(capped)])

    # The cap is per user, not global.
    other = d2.create_user("other@b.c", "correct horse")["user_id"]
    check("another user is unaffected by the first's cap",
          d2.create_stream(other, "theirs")["stream_id"] > 0)

    # Labels are bounded by the column width, so an over-long one is a clean
    # rejection rather than a truncation or a driver-specific error.
    check("an over-long label is refused",
          raises(ValueError, d2.create_stream, other, "x" * (m.MAX_STREAM_LABEL_LENGTH + 1)))
    check("a label at exactly the limit is accepted",
          d2.create_stream(other, "x" * m.MAX_STREAM_LABEL_LENGTH)["stream_id"] > 0)
    check("a whitespace-only label becomes None",
          d2.create_stream(other, "   ")["label"] is None)

    print("\n" + "=" * 60)
    print(f"{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


sys.exit(main())
