"""
Flight lifecycle, against a fake runtime and a fake db-writer.

The orchestrator's hard part is not starting containers — it is the event sequences
MediaMTX actually produces: duplicate hooks, an offline immediately followed by an
online, a key revoked mid-flight, a container that fails to start. None of those need
Docker to exercise, and all of them are where a bug leaves either a GPU container
running forever or a flight row that never closes.

Needs: nothing. See README.md.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.environ.get(
    "ORCHESTRATOR_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "orchestrator"),
))

from flights import FlightOrchestrator, build_flight_env  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


# ── Fakes ─────────────────────────────────────────────────────────────────────

KEY_A = "aaaaaaaaaaaaaaaa"
KEY_B = "bbbbbbbbbbbbbbbb"
DEAD_KEY = "dddddddddddddddd"


class FakeRuntime:
    def __init__(self, fail_on=None):
        self.started = []     # (flight_id, env)
        self.stopped = []     # handles
        self.running = set()
        self._fail_on = fail_on or set()
        self._n = 0

    def start(self, flight_id, env):
        if flight_id in self._fail_on:
            raise RuntimeError("simulated container start failure")
        self._n += 1
        handle = f"handle-{self._n}"
        self.started.append((flight_id, env))
        self.running.add(handle)
        return handle

    def stop(self, handle):
        self.stopped.append(handle)
        self.running.discard(handle)


class FakeDirectory:
    def __init__(self):
        self.opened = []
        self.closed = []
        self._next_id = 1

    async def open_flight(self, stream_key):
        if stream_key == DEAD_KEY:
            return None
        flight_id = self._next_id
        self._next_id += 1
        uuid = f"uuid-{flight_id}"
        self.opened.append(stream_key)
        return {
            "flight_id": flight_id,
            "public_uuid": uuid,
            "stream_id": 1,
            "user_id": 1,
            "ingest_path": f"in/{stream_key}",
            "output_path": f"out/{uuid}",
            "publisher_token": f"token-{flight_id}",
        }

    async def close_flight(self, flight_id, publisher_token):
        self.closed.append((flight_id, publisher_token))
        return True


def make(grace=0.15, fail_on=None, base_env=None):
    rt = FakeRuntime(fail_on=fail_on)
    d = FakeDirectory()
    return rt, d, FlightOrchestrator(rt, d, base_env or {}, grace)


# ── The env contract ──────────────────────────────────────────────────────────

opened = {
    "flight_id": 7, "public_uuid": "u7", "publisher_token": "tok",
    "ingest_path": "in/key", "output_path": "out/u7",
}
env = build_flight_env(opened, {"DB_WRITER_URL": "http://db-writer:8000", "MODE": "danger"})

check("flight_id injected", env["FLIGHT_ID"] == "7")
check("publisher token injected", env["PUBLISHER_TOKEN"] == "tok")
check("reader path injected", env["VIDEO_STREAM_READER_STREAM_KEY"] == "in/key")
check("output path injected", env["VIDEO_OUT_STREAM_STREAM_KEY"] == "out/u7")
check("operator settings forwarded", env["MODE"] == "danger")
check("NO end-user credentials in the container env",
      not any(k in env for k in ("DB_USERNAME", "DB_PASSWORD")))

hijack = build_flight_env(opened, {"VIDEO_OUT_STREAM_STREAM_KEY": "out/somebody-else"})
check("per-flight output path overrides a stray base value",
      hijack["VIDEO_OUT_STREAM_STREAM_KEY"] == "out/u7")


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def scenarios():
    # A stream goes live, then stops.
    rt, d, o = make()
    fid = await o.stream_online(KEY_A)
    check("online opens a flight and starts a container",
          fid == 1 and len(rt.started) == 1 and o.active == 1)
    check("container env carries this flight's paths",
          rt.started[0][1]["VIDEO_STREAM_READER_STREAM_KEY"] == f"in/{KEY_A}")

    await o.stream_offline(KEY_A)
    check("teardown is deferred, container still up", len(rt.stopped) == 0)
    await asyncio.sleep(0.3)
    check("container stopped after the grace period", len(rt.stopped) == 1)
    check("flight closed in db-writer", d.closed == [(1, "token-1")])
    check("no flights left tracked", o.active == 0)

    # A duplicate online hook must not start a second container.
    rt, d, o = make()
    await o.stream_online(KEY_A)
    second = await o.stream_online(KEY_A)
    check("duplicate online is a no-op", len(rt.started) == 1 and second == 1)
    check("no second flight opened", len(d.opened) == 1)

    # The case the grace period exists for: a blip, not a landing.
    rt, d, o = make()
    await o.stream_online(KEY_A)
    await o.stream_offline(KEY_A)
    await asyncio.sleep(0.05)
    again = await o.stream_online(KEY_A)
    await asyncio.sleep(0.3)
    check("reconnect within grace keeps the SAME flight", again == 1)
    check("reconnect does not stop the container", len(rt.stopped) == 0)
    check("reconnect does not open a second flight", len(d.opened) == 1)
    check("reconnect does not close the flight", d.closed == [])
    check("flight still active after the grace window", o.active == 1)

    # A reconnect after the grace window is a new flight, not the old one.
    rt, d, o = make(grace=0.05)
    await o.stream_online(KEY_A)
    await o.stream_offline(KEY_A)
    await asyncio.sleep(0.2)
    new = await o.stream_online(KEY_A)
    check("reconnect after grace opens a NEW flight", new == 2)
    check("the first container was stopped", len(rt.stopped) == 1)
    check("two containers started in total", len(rt.started) == 2)

    # Duplicate offline hooks must collapse into one teardown.
    rt, d, o = make()
    await o.stream_online(KEY_A)
    await o.stream_offline(KEY_A)
    await o.stream_offline(KEY_A)
    await asyncio.sleep(0.3)
    check("duplicate offline tears down once", len(rt.stopped) == 1)
    check("duplicate offline closes the flight once", len(d.closed) == 1)

    # Offline for a stream that was never online.
    rt, d, o = make()
    await o.stream_offline(KEY_A)
    await asyncio.sleep(0.2)
    check("offline with no flight is ignored",
          len(rt.stopped) == 0 and len(d.closed) == 0)

    # Revoked between MediaMTX accepting the publisher and the stream going live.
    rt, d, o = make()
    dead = await o.stream_online(DEAD_KEY)
    check("dead key starts nothing", dead is None and len(rt.started) == 0)
    check("dead key leaves no flight tracked", o.active == 0)

    # The container will not start. The flight row must not be left looking live.
    rt, d, o = make(fail_on={1})
    failed = await o.stream_online(KEY_A)
    check("failed container start returns no flight", failed is None)
    check("failed container start CLOSES the orphaned flight row",
          d.closed == [(1, "token-1")])
    check("failed container start leaves nothing tracked", o.active == 0)

    # Two tenants at once.
    rt, d, o = make()
    a = await o.stream_online(KEY_A)
    b = await o.stream_online(KEY_B)
    check("two streams produce two independent flights", a == 1 and b == 2 and o.active == 2)
    await o.stream_offline(KEY_A)
    await asyncio.sleep(0.3)
    check("tearing one down leaves the other running", o.active == 1)
    check("only the stopped flight was closed", d.closed == [(1, "token-1")])

    # grace=0 tears down inline.
    rt, d, o = make(grace=0)
    await o.stream_online(KEY_A)
    await o.stream_offline(KEY_A)
    check("grace=0 tears down immediately",
          len(rt.stopped) == 1 and len(d.closed) == 1 and o.active == 0)

    # The orchestrator itself going down must not strand GPU containers.
    rt, d, o = make()
    await o.stream_online(KEY_A)
    await o.stream_online(KEY_B)
    await o.shutdown()
    check("shutdown stops every running container", len(rt.stopped) == 2)
    check("shutdown closes every flight", len(d.closed) == 2)
    check("shutdown leaves nothing running", len(rt.running) == 0 and o.active == 0)

    # A pending teardown must not survive shutdown as a live container either.
    rt, d, o = make(grace=10)
    await o.stream_online(KEY_A)
    await o.stream_offline(KEY_A)
    await o.shutdown()
    check("shutdown during a pending teardown still stops the container",
          len(rt.stopped) == 1 and len(rt.running) == 0)


asyncio.run(scenarios())

passed = sum(1 for _, ok, _ in results if ok)
print("\n" + "=" * 58)
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
