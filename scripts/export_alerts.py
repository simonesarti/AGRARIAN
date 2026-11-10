"""
Export alerts from the database to a local folder.

Each alert's image is written as a JPEG and its fields are collected in a
metadata file next to the images:

    <out_dir>/
        flight_<flight_id>/
            alert_<alert_id>_frame_<frame_id>.jpg
        alerts.csv          # one row per alert, with the relative image path
                            # (alerts_flight_<id>.csv when --flight-id is used)

Images are streamed with a server-side cursor, so memory use stays flat no matter
how many alerts the table holds. Re-running skips images that already exist, so an
interrupted export can be resumed.

The password is read from the PGPASSWORD environment variable, or prompted for.

Usage:
    python3 scripts/export_alerts.py --host 10.160.101.65 --dbname pilot_greece_db \
        --user pilot_greece --out-dir alerts_export
    python3 scripts/export_alerts.py ... --flight-id 12     # a single flight
"""

import argparse
import base64
import binascii
import csv
import getpass
import os
import sys
from pathlib import Path

import psycopg2

JPEG_MAGIC = b"\xff\xd8\xff"
FETCH_BATCH = 50  # rows per round-trip; each row carries one full image

CSV_FIELDS = [
    "alert_id", "flight_id", "frame_id", "alert_msg", "timestamp", "datetime",
    "latitude", "longitude", "image_width", "image_height", "image_file",
]


def decode_image(data) -> bytes | None:
    """
    Return JPEG bytes from an image_data value.
    db_writer stores raw JPEG bytes; base64 text (as bytes or str) is also accepted.
    Returns None for empty values or data that is neither.
    """
    if data is None:
        return None
    raw = bytes(data) if isinstance(data, memoryview) else data
    if isinstance(raw, str):
        raw = raw.encode("ascii", errors="ignore")
    if raw.startswith(JPEG_MAGIC):
        return raw
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    return decoded if decoded.startswith(JPEG_MAGIC) else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Export alerts (images + metadata) from the database.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--dbname", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--out-dir", default="alerts_export", help="Destination folder (default: alerts_export)")
    parser.add_argument("--flight-id", type=int, default=None, help="Export only this flight")
    args = parser.parse_args()

    password = os.environ.get("PGPASSWORD") or getpass.getpass("DB password: ")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # A filtered export gets its own metadata file so it never overwrites the full one.
    csv_path = out_dir / ("alerts.csv" if args.flight_id is None else f"alerts_flight_{args.flight_id}.csv")

    query = """
        SELECT alert_id, flight_id, frame_id, alert_msg, timestamp, datetime,
               latitude, longitude, image_width, image_height, image_data
        FROM alerts
    """
    params: tuple = ()
    if args.flight_id is not None:
        query += " WHERE flight_id = %s"
        params = (args.flight_id,)
    query += " ORDER BY flight_id, alert_id"

    conn = psycopg2.connect(
        host=args.host, port=args.port, dbname=args.dbname, user=args.user, password=password,
    )
    written = skipped = missing = 0
    try:
        # Named cursor = server-side: rows are fetched in batches, not all at once.
        # It must live inside a transaction, which psycopg2 opens implicitly.
        with conn.cursor(name="export_alerts") as cur, \
                open(csv_path, "w", newline="", encoding="utf-8") as csv_file:
            cur.itersize = FETCH_BATCH
            cur.execute(query, params)
            writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
            writer.writeheader()

            for (alert_id, flight_id, frame_id, alert_msg, timestamp, dt,
                 latitude, longitude, width, height, image_data) in cur:

                rel_path = Path(f"flight_{flight_id}") / f"alert_{alert_id}_frame_{frame_id}.jpg"
                img_path = out_dir / rel_path

                if img_path.exists():
                    skipped += 1
                else:
                    jpeg = decode_image(image_data)
                    if jpeg is None:
                        missing += 1
                        rel_path = None
                        print(f"alert {alert_id}: no decodable image", file=sys.stderr)
                    else:
                        img_path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = img_path.with_suffix(".part")
                        tmp.write_bytes(jpeg)
                        tmp.replace(img_path)  # no half-written .jpg if interrupted
                        written += 1

                writer.writerow({
                    "alert_id": alert_id,
                    "flight_id": flight_id,
                    "frame_id": frame_id,
                    "alert_msg": alert_msg,
                    "timestamp": timestamp,
                    "datetime": dt.isoformat() if dt is not None else "",
                    "latitude": latitude,
                    "longitude": longitude,
                    "image_width": width,
                    "image_height": height,
                    "image_file": rel_path.as_posix() if rel_path else "",
                })

                total = written + skipped + missing
                if total % 100 == 0:
                    print(f"{total} alerts processed ...")
    finally:
        conn.close()

    print(
        f"Done: {written} images written, {skipped} already present, "
        f"{missing} without a usable image. Metadata: {csv_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
