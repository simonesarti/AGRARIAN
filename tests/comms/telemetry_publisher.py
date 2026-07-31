#!/usr/bin/env python3
"""A drone's telemetry, so that the Mosquitto plane carries something real.

run_mqtt_auth.sh proves the broker's authorisation — who may publish where, and
that one flight cannot read another's topics. It proves nothing about the plane
as a *pipe*, because it publishes single values with mosquitto_pub and no app is
listening. This publisher is the other half: a persistent client that streams
the four fields the pipeline actually consumes, fast enough for the combiner to
match them to frames.

Fast enough is a hard number. FRAMETELCOMB_MAX_TIME_DIFF is 150 ms, so a frame
takes the nearest telemetry snapshot only if one landed within that window.
Publishing at 2 Hz would connect, authenticate, deliver, and still leave every
frame unmatched — the pipeline would run with telemetry=None throughout and look
from the outside exactly like a broker that was never reached. The default here
is 10 Hz for that reason, not for realism.

One connection, held open, for all four topics. mosquitto_pub in a shell loop
would reconnect per message, which turns a 10 Hz publisher into 40 authentication
round-trips per second against db-writer and measures the auth endpoint rather
than the telemetry path.

Credential: a drone authenticates with its stream key as the MQTT username (the
password is unused — the key is the secret). That is the same credential it
publishes video with, and Mosquitto's ACL admits it to telemetry/<stream_key>/#
and nothing else.

Usage:
    python telemetry_publisher.py --host mosquitto --stream-key <key> [--hz 10]
"""

import argparse
import asyncio
import math
import sys
import time

from aiomqtt import Client, MqttError

# The four fields TELEMETRY_FIELDS names in app/shared/processes/constants.py.
# Duplicated rather than imported: this runs inside the app image but must not
# depend on the app package being importable from the test's working directory.
FIELDS = ("latitude", "longitude", "rel_alt", "gb_yaw")


async def fly(args: argparse.Namespace) -> None:
    """Publish until killed, reconnecting if the broker goes away."""
    topics = {f: f"telemetry/{args.stream_key}/{f}" for f in FIELDS}
    period = 1.0 / args.hz
    started = time.monotonic()
    sent = 0

    while True:
        try:
            async with Client(
                hostname=args.host,
                port=args.port,
                username=args.stream_key,
                password="unused",
            ) as client:
                print(f"connected to {args.host}:{args.port} as {args.stream_key}", flush=True)
                print(f"publishing {list(topics.values())} at {args.hz} Hz", flush=True)

                while True:
                    # A slow circle rather than a fixed point. A stationary drone
                    # would let the GeoWorker's DEM window cache answer every
                    # frame from its first extraction, so movement is what keeps
                    # the geo path doing work on more than one frame.
                    t = time.monotonic() - started
                    angle = (t / args.circle_period_s) * 2 * math.pi
                    lat = args.lat + args.radius_deg * math.sin(angle)
                    lon = args.lon + args.radius_deg * math.cos(angle)
                    yaw = (math.degrees(angle) + args.gb_yaw) % 360.0

                    values = {
                        "latitude": lat,
                        "longitude": lon,
                        "rel_alt": args.rel_alt,
                        "gb_yaw": yaw,
                    }
                    for field, topic in topics.items():
                        await client.publish(topic, payload=f"{values[field]:.9f}", qos=args.qos)

                    sent += 1
                    if sent % (int(args.hz) * 10 or 1) == 0:
                        print(f"{sent} snapshots sent ({t:.0f}s)", flush=True)
                    await asyncio.sleep(period)

        except MqttError as e:
            # Loud rather than silent: a publisher that quietly retries forever
            # is indistinguishable from one that is working, and the test above
            # would read "no telemetry" as a pipeline fault instead of a broker
            # refusal. Authorisation failures land here.
            print(f"MQTT error: {e} — retrying in 2s", file=sys.stderr, flush=True)
            await asyncio.sleep(2.0)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", required=True, help="MQTT broker hostname")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--stream-key", required=True, help="MQTT username and topic namespace")
    p.add_argument("--hz", type=float, default=10.0, help="snapshots per second (>6.7 to beat the 150 ms match window)")
    p.add_argument("--qos", type=int, default=1, choices=(0, 1, 2))
    # Genoa, matching TELEMETRY_LISTENER_TEMPLATE_TELEMETRY.
    p.add_argument("--lat", type=float, default=44.414622942776454)
    p.add_argument("--lon", type=float, default=8.880484631296774)
    p.add_argument("--rel-alt", type=float, default=40.0)
    p.add_argument("--gb-yaw", type=float, default=270.0)
    p.add_argument("--radius-deg", type=float, default=0.0002, help="circle radius in degrees (~22 m)")
    p.add_argument("--circle-period-s", type=float, default=60.0)
    args = p.parse_args()

    try:
        asyncio.run(fly(args))
    except KeyboardInterrupt:
        print("stopped", flush=True)


if __name__ == "__main__":
    main()
