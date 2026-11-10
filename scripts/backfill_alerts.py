"""
One-off backfill: insert test flights and their alerts (with screenshot images) into the DB.

Each entry in ALERTS below becomes one alert row. Entries sharing the same `flight` label
are grouped into one new flight row; the DB assigns the new flight_ids.

What the script fills in, matching what the app itself writes:
  - flights.user_id / stream_url : copied from the most recent existing flight
  - flights.start_time           : the flight's first alert time (UTC, like the app)
  - alerts.timestamp             : Unix epoch of the alert time
  - alerts.datetime              : same instant, using the convention detected from
                                   existing rows (the app container runs in UTC)
  - alerts.image_*               : screenshot resized to 1920x1080, JPEG quality 85
  - alerts.frame_id              : NULL (no real frame id exists for a screenshot)

Everything runs in one transaction, after a preview and a typed "yes". Re-running is
refused if alerts with the same timestamps already exist.

Usage (no install needed; uv provides the dependencies):
    read -rsp "DB password: " PGPASSWORD && export PGPASSWORD && echo
    uv run --with psycopg2-binary --with pillow scripts/backfill_alerts.py
    unset PGPASSWORD

    --dry-run   preview only, never writes
"""

import argparse
import getpass
import io
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
from PIL import Image

# ------------------------------------------------------------------ configuration

DB = dict(host="10.160.101.65", port=5432, dbname="pilot_greece_db", user="pilot_greece")

SCREENSHOT_DIR = Path("/home/simone/Pictures/Screenshots")
LOCAL_TZ = ZoneInfo("Europe/Athens")       # alert times below are Greek local time
TARGET_WH = (1920, 1080)                   # VIDEO_STREAM_READER_ORIGINAL_SHAPE
JPEG_QUALITY = 85                          # ALERTS_JPEG_COMPRESSION_QUALITY

# (flight label, latitude, longitude, alert_msg, local time on 2026-08-09, screenshot file)
ALERTS = [
    ("flight1", 35.427102, 24.173983, "Vehicle",                          "10:51", "Screenshot From 2026-11-09 16-52-21.png"),
    ("flight1", 35.427069, 24.173901, "Vehicle",                          "10:52", "Screenshot From 2026-11-09 16-52-57.png"),
    ("flight2", 35.427252, 24.173836, "Out of Geofenced Area",            "10:56", "Screenshot From 2026-11-09 16-54-15.png"),
    ("flight2", 35.427059, 24.173872, "Vehicle & Out of Geofenced Area",  "10:57", "Screenshot From 2026-11-09 16-54-46.png"),
    ("flight3", 35.426927, 24.174062, "Steep Slope",                      "11:04", "Screenshot From 2026-11-09 16-55-46.png"),
]
ALERT_DATE = (2026, 8, 9)

# ------------------------------------------------------------------ helpers


def to_jpeg(path: Path) -> bytes:
    """Load a screenshot, resize to TARGET_WH if needed, and encode as JPEG."""
    with Image.open(path) as img:
        img = img.convert("RGB")
        if img.size != TARGET_WH:
            img = img.resize(TARGET_WH, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()


def alert_instant(hhmm: str) -> datetime:
    """Greek local HH:MM on ALERT_DATE -> timezone-aware UTC datetime."""
    h, m = map(int, hhmm.split(":"))
    return datetime(*ALERT_DATE, h, m, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def detect_datetime_offset_s(cur) -> int:
    """
    How existing rows store alerts.datetime relative to UTC, in seconds
    (0 = naive UTC, 10800 = naive UTC+3, ...). Rounded to 15 minutes; most common wins.
    """
    cur.execute("""
        SELECT EXTRACT(EPOCH FROM (datetime - (to_timestamp(timestamp) AT TIME ZONE 'UTC')))
        FROM alerts
        WHERE timestamp IS NOT NULL AND datetime IS NOT NULL
        ORDER BY alert_id DESC LIMIT 50
    """)
    offsets = [round(float(r[0]) / 900) * 900 for r in cur.fetchall()]
    return Counter(offsets).most_common(1)[0][0] if offsets else 0


# ------------------------------------------------------------------ main


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill test flights and alerts.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, never write")
    args = parser.parse_args()

    # Load and encode every image up front, so a missing file aborts before touching the DB.
    rows = []
    for flight, lat, lon, msg, hhmm, fname in ALERTS:
        path = SCREENSHOT_DIR / fname
        if not path.is_file():
            print(f"Missing screenshot: {path}", file=sys.stderr)
            return 1
        with Image.open(path) as img:
            src_wh = img.size
        rows.append(dict(flight=flight, lat=lat, lon=lon, msg=msg, when=alert_instant(hhmm),
                         jpeg=to_jpeg(path), src_wh=src_wh, fname=fname))

    password = os.environ.get("PGPASSWORD") or getpass.getpass("DB password: ")
    conn = psycopg2.connect(password=password, **DB)
    try:
        with conn.cursor() as cur:
            # ---- values copied from existing data ----
            cur.execute("SELECT flight_id, user_id, stream_url FROM flights ORDER BY flight_id DESC LIMIT 1")
            latest = cur.fetchone()
            if latest is None:
                print("No existing flight to copy user_id / stream_url from.", file=sys.stderr)
                return 1
            latest_flight_id, user_id, _ = latest
            cur.execute("""
                SELECT stream_url FROM flights WHERE stream_url IS NOT NULL
                ORDER BY flight_id DESC LIMIT 1
            """)
            r = cur.fetchone()
            stream_url = r[0] if r else None
            cur.execute("SELECT COUNT(DISTINCT stream_url) FROM flights WHERE stream_url IS NOT NULL")
            n_urls = cur.fetchone()[0]

            offset_s = detect_datetime_offset_s(cur)

            # ---- refuse to run twice ----
            timestamps = [row["when"].timestamp() for row in rows]
            cur.execute("SELECT COUNT(*) FROM alerts WHERE timestamp = ANY(%s)", (timestamps,))
            if cur.fetchone()[0]:
                print("Alerts with these timestamps already exist — backfill was already run. Aborting.",
                      file=sys.stderr)
                return 1

            # ---- preview ----
            print(f"user_id    : {user_id} (from latest flight {latest_flight_id})")
            print(f"stream_url : {stream_url!r}" + (f"  [note: table has {n_urls} distinct URLs; using the latest]" if n_urls > 1 else ""))
            print(f"datetime   : stored as UTC{offset_s / 3600:+.2g}h, detected from existing alerts")
            print()
            flights = list(dict.fromkeys(row["flight"] for row in rows))
            for label in flights:
                group = [row for row in rows if row["flight"] == label]
                print(f"new flight ({label}), start {group[0]['when']:%Y-%m-%d %H:%M} UTC")
                for row in group:
                    local = row["when"].astimezone(LOCAL_TZ)
                    print(f"   {local:%H:%M} Athens  {row['lat']:.7f}, {row['lon']:.7f}  "
                          f"{row['msg']!r:36}  {row['src_wh'][0]}x{row['src_wh'][1]} -> "
                          f"{TARGET_WH[0]}x{TARGET_WH[1]}, {len(row['jpeg']) / 1024:.0f} KB  [{row['fname']}]")
            print()

            if args.dry_run:
                print("Dry run: nothing written.")
                conn.rollback()
                return 0
            if input(f"Insert {len(flights)} flights and {len(rows)} alerts? Type yes: ").strip() != "yes":
                print("Aborted, nothing written.")
                conn.rollback()
                return 0

            # ---- insert ----
            for label in flights:
                group = [row for row in rows if row["flight"] == label]
                start_naive_utc = group[0]["when"].replace(tzinfo=None)
                cur.execute(
                    "INSERT INTO flights (user_id, start_time, stream_url) VALUES (%s, %s, %s) RETURNING flight_id",
                    (user_id, start_naive_utc, stream_url),
                )
                flight_id = cur.fetchone()[0]
                alert_ids = []
                for row in group:
                    stored_dt = row["when"].replace(tzinfo=None) + timedelta(seconds=offset_s)
                    cur.execute(
                        """INSERT INTO alerts (flight_id, alert_msg, frame_id, timestamp, datetime,
                                               image_data, image_width, image_height, latitude, longitude)
                           VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s) RETURNING alert_id""",
                        (flight_id, row["msg"], row["when"].timestamp(), stored_dt,
                         psycopg2.Binary(row["jpeg"]), TARGET_WH[0], TARGET_WH[1], row["lat"], row["lon"]),
                    )
                    alert_ids.append(cur.fetchone()[0])
                print(f"{label} -> flight_id {flight_id}, alert_ids {alert_ids}")
        conn.commit()
        print("Committed.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
