"""
Flight history reads, against SQLite in memory.

Needs no running stack, like test_schema.py — this is the query layer the portal's
history page rests on, and every property worth pinning is a property of the query
rather than of the HTTP around it:

  paging      by cursor, so a flight starting mid-browse cannot duplicate a row
  counting    alerts and recordings on the same flight, neither inflating the other
  ownership   another tenant's flight is indistinguishable from one that never was

The third is the one that matters most: alert crops are photographs of somebody's
land, and alert_ids are sequential across every tenant in the system.

Run:  see README.md
"""
import os
import sys
from datetime import datetime, timedelta, timezone

# db_writer is not a package on the path; point at it explicitly so this file can be
# run from anywhere. DB_WRITER_DIR overrides for a container mount.
sys.path.insert(0, os.environ.get(
    "DB_WRITER_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "db_writer")))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db_manager as m

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def build():
    """A UserDirectory bound to a throwaway in-memory database."""
    d = m.UserDirectory.__new__(m.UserDirectory)      # skip __init__; no real DB URL
    d._engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(d._engine)
    return d, sessionmaker(bind=d._engine)


directory, Sessions = build()

T0 = datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc)


def add_flight(stream_id, minutes_in, duration_min=None, alerts=0, recordings=0,
               with_image=True):
    """One flight plus its children, at a fixed time so durations are assertable."""
    with Sessions() as s:
        start = T0 + timedelta(minutes=minutes_in)
        flight = m.Flight(stream_id=stream_id, start_time=start)
        s.add(flight)
        s.flush()
        flight.output_path = m.output_path_for(flight.public_uuid)
        if duration_min is not None:
            flight.end_time = start + timedelta(minutes=duration_min)
        for i in range(alerts):
            s.add(m.Alert(
                flight_id=flight.flight_id, alert_msg=f"danger {i}", frame_id=i,
                timestamp=float(i), datetime=start + timedelta(seconds=i),
                image_data=b"\xff\xd8jpeg-bytes" if with_image else None,
                image_width=320, image_height=240))
        for i in range(recordings):
            s.add(m.Recording(
                flight_id=flight.flight_id, segment_path=f"/rec/{flight.public_uuid}/{i}.mp4",
                storage_backend="azure", storage_location=f"blob/{flight.flight_id}/{i}.mp4"))
        s.commit()
        return flight.flight_id


# ── Two tenants, so every read below has something it must not return ────────

alice = directory.create_user("alice@test.io", "correct horse")["user_id"]
mallory = directory.create_user("mallory@test.io", "correct horse")["user_id"]

north = directory.create_stream(alice, "north field")["stream_id"]
south = directory.create_stream(alice, "south field")["stream_id"]
theirs = directory.create_stream(mallory, "not alice's")["stream_id"]

# Alice: 5 on north, 2 on south. Mallory: 1. Flight ids ascend with start time.
alice_flights = [add_flight(north, i * 60, duration_min=30) for i in range(3)]
counted = add_flight(north, 300, duration_min=45, alerts=3, recordings=2)
alice_flights.append(counted)
alice_flights.append(add_flight(north, 360))                    # still open
south_flights = [add_flight(south, 400 + i, duration_min=5) for i in range(2)]
mallory_flight = add_flight(theirs, 500, duration_min=10, alerts=4, recordings=1)

# ── The page itself ──────────────────────────────────────────────────────────

page = directory.flight_history(alice, limit=50)
ids = [f["flight_id"] for f in page["flights"]]

check("history returns every flight of the user's, across all their slots",
      sorted(ids) == sorted(alice_flights + south_flights), str(ids))
check("...and nobody else's — the other tenant's flight is absent",
      mallory_flight not in ids, str(ids))
check("newest first", ids == sorted(ids, reverse=True), str(ids))
check("a page that reached the end offers no cursor", page["next_before"] is None,
      str(page["next_before"]))

row = [f for f in page["flights"] if f["flight_id"] == counted][0]
check("each row carries the slot it flew on, by id and by label",
      row["stream_id"] == north and row["label"] == "north field", str(row))
check("a closed flight reports its end time", row["end_time"] is not None, str(row["end_time"]))

still_open = [f for f in page["flights"] if f["flight_id"] == alice_flights[-1]][0]
check("an open flight reports end_time as null rather than a guess",
      still_open["end_time"] is None, str(still_open["end_time"]))

# ── Counts: the join that would have multiplied them ─────────────────────────
#
# `counted` has 3 alerts and 2 recordings. Both hang off flights, so a single
# query joining flights to both would produce 6 rows and report 6 of each. The
# counts are done per child table for exactly this reason.

check("alerts are counted per flight", row["alert_count"] == 3, str(row["alert_count"]))
check("recordings are counted separately, not multiplied by the alerts",
      row["recording_count"] == 2, str(row["recording_count"]))
check("a flight with neither reports zero rather than omitting the count",
      all(f["alert_count"] == 0 and f["recording_count"] == 0
          for f in page["flights"] if f["flight_id"] != counted),
      str([(f["flight_id"], f["alert_count"]) for f in page["flights"]]))

with Sessions() as s:
    both = (s.query(m.Flight.flight_id,
                    m.func.count(m.Alert.alert_id), m.func.count(m.Recording.recording_id))
            .outerjoin(m.Alert, m.Alert.flight_id == m.Flight.flight_id)
            .outerjoin(m.Recording, m.Recording.flight_id == m.Flight.flight_id)
            .filter(m.Flight.flight_id == counted)
            .group_by(m.Flight.flight_id).first())
check("the control: counting both in one joined query DOES inflate them",
      both[1] != 3 and both[2] != 2, f"one join gave {both[1]} alerts, {both[2]} recordings")

# ── Paging ───────────────────────────────────────────────────────────────────

ALL_ALICE = sorted(alice_flights + south_flights)          # seven, across two slots

first = directory.flight_history(alice, limit=4)
check("a page is as long as asked for", len(first["flights"]) == 4, str(len(first["flights"])))
check("...and hands back a cursor when more remain",
      first["next_before"] == first["flights"][-1]["flight_id"], str(first["next_before"]))

second = directory.flight_history(alice, limit=4, before=first["next_before"])
check("the next page continues below the cursor, with no row repeated",
      not (set(f["flight_id"] for f in first["flights"])
           & set(f["flight_id"] for f in second["flights"])),
      str([f["flight_id"] for f in second["flights"]]))

check("the two pages together are the whole history",
      sorted([f["flight_id"] for f in first["flights"]]
             + [f["flight_id"] for f in second["flights"]]) == ALL_ALICE,
      str([f["flight_id"] for f in second["flights"]]))
check("the last page offers no cursor onward", second["next_before"] is None,
      str(second["next_before"]))

# A page filled exactly to the limit with nothing beyond it must NOT offer a
# cursor: the extra row fetched is what distinguishes "full" from "more".
exact = directory.flight_history(alice, limit=len(ALL_ALICE))
check("a page that exactly consumes the history offers no cursor to an empty one",
      exact["next_before"] is None, str(exact["next_before"]))

# ── Why the cursor is a flight_id and not an offset ──────────────────────────
#
# The falsification: a flight starts while the user is reading page one. With
# OFFSET paging every later row shifts down by one and page two repeats the row
# page one ended on. This asserts the cursor does not — and, below it, that the
# offset really would have, so the property is a fact about this data rather
# than a claim about paging in general.

page_one = directory.flight_history(alice, limit=4)
interloper = add_flight(north, 600)                  # newest of all, id above every other
page_two = directory.flight_history(alice, limit=4, before=page_one["next_before"])
across = ([f["flight_id"] for f in page_one["flights"]]
          + [f["flight_id"] for f in page_two["flights"]])

check("a flight starting mid-browse does not repeat a row onto the next page",
      len(across) == len(set(across)),
      f"{[f['flight_id'] for f in page_one['flights']]} then "
      f"{[f['flight_id'] for f in page_two['flights']]}")
check("...and does not hide one either: the two pages are exactly the history as "
      "it stood when page one was read",
      sorted(across) == ALL_ALICE and interloper not in across, str(sorted(across)))

with Sessions() as s:
    offset_page_two = [
        f.flight_id for f, _ in
        s.query(m.Flight, m.Stream)
        .join(m.Stream, m.Flight.stream_id == m.Stream.stream_id)
        .filter(m.Stream.user_id == alice)
        .order_by(m.Flight.flight_id.desc())
        .offset(4).limit(4).all()
    ]
check("the control: OFFSET paging on the same data DOES repeat a row",
      bool(set(f["flight_id"] for f in page_one["flights"]) & set(offset_page_two)),
      f"offset gave {offset_page_two}")

# ── Limits ───────────────────────────────────────────────────────────────────
#
# A user with more flights than the ceiling, or the clamp below is asserted
# against data too small to reach it.

busy = directory.create_user("busy@test.io", "correct horse")["user_id"]
busy_slot = directory.create_stream(busy, "flies daily")["stream_id"]
with Sessions() as s:
    for i in range(m.MAX_FLIGHT_HISTORY_PAGE_SIZE + 2):
        s.add(m.Flight(stream_id=busy_slot, start_time=T0 + timedelta(minutes=i)))
    s.commit()

huge = directory.flight_history(busy, limit=10 ** 6)
check("an enormous limit is clamped to the ceiling rather than honoured",
      len(huge["flights"]) == m.MAX_FLIGHT_HISTORY_PAGE_SIZE, str(len(huge["flights"])))
check("...and the clamped page still says there is more behind it",
      huge["next_before"] is not None, str(huge["next_before"]))

check("a limit of zero still returns a row rather than an empty page forever",
      len(directory.flight_history(alice, limit=0)["flights"]) == 1)
check("a negative limit does the same", len(directory.flight_history(alice, limit=-5)["flights"]) == 1)

# ── Narrowing to one slot ────────────────────────────────────────────────────

one_slot = directory.flight_history(alice, limit=50, stream_id=south)
check("filtering by slot returns that slot's flights only",
      sorted(f["flight_id"] for f in one_slot["flights"]) == sorted(south_flights),
      str([f["flight_id"] for f in one_slot["flights"]]))

check("filtering by ANOTHER tenant's slot returns nothing, not their flights",
      directory.flight_history(alice, limit=50, stream_id=theirs)["flights"] == [])

check("a user with no flights gets an empty page and no cursor",
      directory.flight_history(directory.create_user("new@test.io", "correct horse")["user_id"])
      == {"flights": [], "next_before": None})

# ── One flight ───────────────────────────────────────────────────────────────

detail = directory.flight_detail(counted, alice)
check("a flight's detail resolves for its owner", detail is not None)
check("it names the slot it flew on", detail["label"] == "north field", str(detail["label"]))
check("it reports the true alert total", detail["alert_total"] == 3, str(detail["alert_total"]))
check("it lists the alerts, newest first",
      [a["alert_msg"] for a in detail["alerts"]] == ["danger 2", "danger 1", "danger 0"],
      str([a["alert_msg"] for a in detail["alerts"]]))
check("each alert says whether it has a crop, without carrying one",
      all(a["has_image"] for a in detail["alerts"])
      and not any("image_data" in a for a in detail["alerts"]),
      str(detail["alerts"][0]))
check("it lists the recordings with where they were archived",
      len(detail["recordings"]) == 2
      and detail["recordings"][0]["storage_backend"] == "azure"
      and detail["recordings"][0]["storage_location"].startswith("blob/"),
      str(detail["recordings"]))
check("the media path is NOT in the detail — history reads what happened, it does "
      "not hand out a way to reach the stream",
      "public_uuid" not in detail and "output_path" not in detail, str(sorted(detail)))

check("another tenant's flight is not found, exactly as an absent one is not",
      directory.flight_detail(mallory_flight, alice) is None
      and directory.flight_detail(10 ** 6, alice) is None)
check("...and the owner can still read it, so the 'not found' is about ownership",
      directory.flight_detail(mallory_flight, mallory) is not None)

# The alert list is capped, and the total is what says so.
big = add_flight(north, 700, duration_min=5, alerts=m.FLIGHT_ALERTS_PAGE_SIZE + 7)
detail = directory.flight_detail(big, alice)
check("a flight with more alerts than fit returns one page of them",
      len(detail["alerts"]) == m.FLIGHT_ALERTS_PAGE_SIZE, str(len(detail["alerts"])))
check("...and reports the total, so a truncated list cannot pass for a whole one",
      detail["alert_total"] == m.FLIGHT_ALERTS_PAGE_SIZE + 7, str(detail["alert_total"]))

# ── Alert crops ──────────────────────────────────────────────────────────────

alert = directory.flight_detail(counted, alice)["alerts"][0]
check("an owner gets the crop bytes",
      directory.alert_image(alert["alert_id"], counted, alice) == b"\xff\xd8jpeg-bytes")

check("another tenant gets nothing for the same alert",
      directory.alert_image(alert["alert_id"], counted, mallory) is None)

# The flight in the URL is checked too, not just the alert id. Without that, a
# user could pair one of their own flight_ids with any alert_id in the system.
check("a real alert id under the WRONG flight is refused, even for the owner",
      directory.alert_image(alert["alert_id"], south_flights[0], alice) is None)

others = directory.flight_detail(mallory_flight, mallory)["alerts"][0]
check("pairing another tenant's alert with one's own flight buys nothing",
      directory.alert_image(others["alert_id"], south_flights[0], alice) is None
      and directory.alert_image(others["alert_id"], mallory_flight, alice) is None)

no_image = add_flight(north, 800, duration_min=1, alerts=1, with_image=False)
none_alert = directory.flight_detail(no_image, alice)["alerts"][0]
check("an alert stored without a crop reports has_image false",
      none_alert["has_image"] is False)
check("...and asking for its bytes is the same nothing as asking for a stranger's",
      directory.alert_image(none_alert["alert_id"], no_image, alice) is None)

# ── Nothing here wrote anything ──────────────────────────────────────────────
#
# History is a read. If one of these methods ever grows a write, this is what
# fails: the row counts are taken again after every read above has run.

with Sessions() as s:
    totals = (s.query(m.Flight).count(), s.query(m.Alert).count(), s.query(m.Recording).count())
directory.flight_history(alice, limit=50)
directory.flight_detail(counted, alice)
directory.alert_image(alert["alert_id"], counted, alice)
with Sessions() as s:
    again = (s.query(m.Flight).count(), s.query(m.Alert).count(), s.query(m.Recording).count())
check("reading history changes nothing in the database", totals == again, f"{totals} → {again}")

# ── The page does not fetch the images it is careful not to return ───────────
#
# flight_detail's docstring has always promised the bytes are absent, and that
# was true of the response and false of the query: session.query(Alert) selects
# every column, so a fifty-alert page pulled fifty full-resolution JPEGs into the
# process to evaluate `image_data is not None` and discard them.
#
# Every assertion above passes either way — a correct answer computed expensively
# is still a correct answer — so the cost needs its own check, and the check has
# to look at the SQL rather than at the result. What is asserted is narrow and
# exact: image_data may appear in a predicate, and must not appear in a select
# list.

from sqlalchemy import event


def statements_for(fn):
    """Every SQL statement SQLAlchemy emits while fn() runs."""
    seen = []
    listener = lambda conn, cur, stmt, params, ctx, many: seen.append(stmt)  # noqa: E731
    event.listen(directory._engine, "before_cursor_execute", listener)
    try:
        fn()
    finally:
        event.remove(directory._engine, "before_cursor_execute", listener)
    return seen


def selects_image_bytes(statements):
    """True if any statement puts image_data in its select list.

    Split on FROM: everything before it is what the database has to materialise
    and send. `image_data IS NOT NULL` lives after it, in the select list only as
    a computed boolean, and is exactly what this must not flag.
    """
    for stmt in statements:
        head = stmt.split(" \nFROM")[0].split(" FROM")[0]
        if "image_data" in head and "IS NOT NULL" not in head:
            return True
    return False


detail_sql = statements_for(lambda: directory.flight_detail(counted, alice))
check("the flight page never selects alert image bytes",
      not selects_image_bytes(detail_sql))
check("...and still answers has_image for every alert on the page",
      all("has_image" in a for a in directory.flight_detail(counted, alice)["alerts"]))

# The control. Without it the assertion above passes against any query that
# happens not to mention the column — including one that selects no alerts at
# all — and would have passed against the mapped-entity query it was written to
# catch only if that query really does name the column. This proves it does.
def _old_query_shape():
    with Sessions() as s:
        (s.query(m.Alert)
          .filter(m.Alert.flight_id == counted)
          .order_by(m.Alert.alert_id.desc())
          .limit(50).all())

check("...and the control: the mapped-entity query it replaced DOES select them",
      selects_image_bytes(statements_for(_old_query_shape)))

# The history list walks flights, not alerts, but it counts them — and a count
# that reached for the rows would be the same defect one level up.
history_sql = statements_for(lambda: directory.flight_history(alice, limit=50))
check("the history list never selects alert image bytes either",
      not selects_image_bytes(history_sql))

# alert_image is the one that must, and asserting so keeps the two checks above
# honest about what they are measuring.
image_sql = statements_for(
    lambda: directory.alert_image(alert["alert_id"], counted, alice))
check("...while alert_image, which serves the crop, does select them",
      selects_image_bytes(image_sql))

print()
print("=" * 60)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
