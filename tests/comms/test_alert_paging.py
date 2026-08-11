"""
Cursor paging over one flight's alerts.

A flight's alert list used to stop at fifty with no way past it: the page said how
many there were and showed the newest fifty, so a truncated list never passed for a
whole one — but the older ones were unreachable. CLOUD_ARCHITECTURE.md §9 carried
that as open, noting the cursor paging was "already written one layer down" for
flights and that this is the same pattern a second time.

It is the same pattern, and the reason for it is stronger here. flight_history's
argument against OFFSET is that a flight taking off mid-browse shifts every later
row down by one. Alerts arrive *while a flight is in the air*, at up to one per
second, so the race OFFSET loses is not a rare interleaving — it is the normal case
for anyone reading the alerts of a flight that has not landed.

The control is what makes that a fact rather than a belief: the same rows are paged
with OFFSET while an alert arrives between the two reads, and the repeated row is
asserted.

Run with no stack:

  docker run --rm -v "$PWD/db_writer:/w:ro" -v "$PWD/tests/comms:/tests:ro" \
    -w /tmp -e DB_WRITER_DIR=/w python:3.11-slim \
    sh -c "pip install -q sqlalchemy bcrypt; python /tests/test_alert_paging.py"
"""
import os, sys
sys.path.insert(0, os.environ["DB_WRITER_DIR"])
import db_manager as m
from db_manager import UserDirectory
from constants import FLIGHT_ALERTS_PAGE_SIZE as PAGE
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

results = []
def check(name, ok, extra=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))

d = UserDirectory.__new__(UserDirectory)
d._engine = create_engine("sqlite:///:memory:")
m.Base.metadata.create_all(d._engine)
S = sessionmaker(bind=d._engine)

alice = d.create_user("a@x.com", "password123")["user_id"]
bob   = d.create_user("b@x.com", "password123")["user_id"]
st    = d.create_stream(alice, label="s")["stream_id"]
fl    = d.open_flight_for_key(d.list_streams(alice)[0]["stream_key"])["flight_id"]

N = PAGE * 2 + 7          # two full pages and a partial one
with S() as s:
    for i in range(N):
        s.add(m.Alert(flight_id=fl, alert_msg=f"a{i}", frame_id=i))
    s.commit()

# Walk every page via the cursor, exactly as the portal does.
seen, cursor, pages = [], None, 0
while True:
    det = d.flight_detail(fl, alice, alerts_before=cursor)
    got = [a["alert_id"] for a in det["alerts"]]
    seen += got
    pages += 1
    cursor = det["next_alerts_before"]
    if cursor is None or pages > 20:
        break

check("every alert is reached exactly once", len(seen) == N and len(set(seen)) == N,
      f"{len(seen)} seen, {len(set(seen))} unique, {N} written")
check("pages are newest-first and strictly descending", seen == sorted(seen, reverse=True))
check("it takes the expected number of pages", pages == 3, f"{pages} pages")
check("alert_total is the whole flight, not the page",
      d.flight_detail(fl, alice)["alert_total"] == N)
check("first page is exactly one page long",
      len(d.flight_detail(fl, alice)["alerts"]) == PAGE)

# The end must be signalled, not guessed: a full last page offering an older link
# leads to an empty page, which reads as a bug rather than as the end.
last = d.flight_detail(fl, alice, alerts_before=seen[PAGE * 2])
check("the final page reports no further cursor", last["next_alerts_before"] is None)

# Exactly PAGE alerts: the boundary case where has_more must be False.
fl2 = d.open_flight_for_key(d.list_streams(alice)[0]["stream_key"])["flight_id"]
with S() as s:
    for i in range(PAGE):
        s.add(m.Alert(flight_id=fl2, alert_msg=f"b{i}", frame_id=i))
    s.commit()
d2 = d.flight_detail(fl2, alice)
check("a flight of exactly one page offers no older link",
      len(d2["alerts"]) == PAGE and d2["next_alerts_before"] is None)

# CONTROL: the whole point of a cursor. Emulate OFFSET paging over the same rows
# while a new alert arrives between pages, and show it repeats a row.
off_p1 = [a["alert_id"] for a in d.flight_detail(fl, alice)["alerts"]]
with S() as s:
    s.add(m.Alert(flight_id=fl, alert_msg="arrived mid-read", frame_id=999))
    s.commit()
with S() as s:
    off_p2 = [r[0] for r in s.query(m.Alert.alert_id)
              .filter(m.Alert.flight_id == fl)
              .order_by(m.Alert.alert_id.desc())
              .limit(PAGE).offset(PAGE).all()]
check("CONTROL: OFFSET repeats a row when an alert arrives mid-read",
      len(set(off_p1) & set(off_p2)) > 0,
      f"{len(set(off_p1) & set(off_p2))} repeated")
cur_p2 = [a["alert_id"] for a in
          d.flight_detail(fl, alice, alerts_before=off_p1[-1])["alerts"]]
check("...and the cursor does not", len(set(off_p1) & set(cur_p2)) == 0)

# Tenancy is unchanged by the new parameter.
check("another tenant still gets nothing, cursor or no cursor",
      d.flight_detail(fl, bob) is None
      and d.flight_detail(fl, bob, alerts_before=seen[0]) is None)

print("\n" + "=" * 60)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
