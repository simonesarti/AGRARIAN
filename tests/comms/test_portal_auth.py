"""
/register, /login and /me over real HTTP against a real database.

Driven by run_portal_auth.sh, which supplies two replica hostnames in DBW1/DBW2.
Two replicas rather than one because the session token is only useful if it is
stateless: the portal will be replicated, and a token minted on one replica has to
be accepted by another with no shared session store between them. That is the
property an in-memory session would quietly break — the same defect ws-server had
with its in-memory client set.
"""
import os
import sys
import urllib.error
import urllib.request
import json

DBW1 = os.environ.get("DBW1", "http://dbw-1:8000")
DBW2 = os.environ.get("DBW2", "http://dbw-2:8000")

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def call(base, path, body=None, token=None, method="POST"):
    """Returns (status, parsed_body). Never raises for an HTTP error status."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


PW = "correct horse"

# ── registration ─────────────────────────────────────────────────────────────
st, body = call(DBW1, "/register", {"email": "alice@test.io", "password": PW})
check("register returns 201", st == 201, str(st))
check("register returns a session token", bool(body.get("session_token")), str(body)[:120])
check("register returns the user_id", isinstance(body.get("user_id"), int))
alice_token = body.get("session_token")
alice_id = body.get("user_id")

st, body = call(DBW1, "/register", {"email": "alice@test.io", "password": PW})
check("a duplicate email is 409, not 500", st == 409, str(st))

st, _ = call(DBW1, "/register", {"email": "ALICE@test.io", "password": PW})
check("the same address in another case is also 409", st == 409, str(st))

st, _ = call(DBW1, "/register", {"email": "bob@test.io", "password": "short"})
check("a too-short password is 400", st == 400, str(st))

st, _ = call(DBW1, "/register", {"email": "notanemail", "password": PW})
check("a malformed email is 400", st == 400, str(st))

st, _ = call(DBW1, "/register", {"email": "bob@test.io", "password": "a" * 73})
check("a password over bcrypt's limit is 400, not 500", st == 400, str(st))

# ── login ────────────────────────────────────────────────────────────────────
st, body = call(DBW1, "/login", {"email": "alice@test.io", "password": PW})
check("login returns 200 and a token", st == 200 and bool(body.get("session_token")), str(st))
check("login resolves the same user_id as registration", body.get("user_id") == alice_id)

st, body = call(DBW1, "/login", {"email": "ALICE@test.io", "password": PW})
check("login works in any casing", st == 200 and body.get("user_id") == alice_id, str(st))

st, body = call(DBW1, "/login", {"email": "alice@test.io", "password": "wrong"})
check("a wrong password is 401", st == 401, str(st))
check("the 401 does not say whether the account exists",
      "credential" in str(body.get("detail", "")).lower(), str(body))

st, body = call(DBW1, "/login", {"email": "nobody@test.io", "password": PW})
check("an unknown address gets the SAME 401 body", st == 401, str(st))

# ── /me, and the cross-replica property ──────────────────────────────────────
st, body = call(DBW1, "/me", token=alice_token, method="GET")
check("/me resolves the session token", st == 200 and body.get("user_id") == alice_id, str(body))

st, body = call(DBW2, "/me", token=alice_token, method="GET")
check("a token minted on replica 1 is accepted on replica 2 — no shared session store",
      st == 200 and body.get("user_id") == alice_id, f"{st} {body}")

st, _ = call(DBW2, "/me", method="GET")
check("/me with no token is 401", st == 401, str(st))

st, _ = call(DBW2, "/me", token="garbage", method="GET")
check("/me with a garbage token is 401", st == 401, str(st))

# A second tenant's token must resolve to that tenant, never to Alice.
st, body = call(DBW2, "/register", {"email": "mallory@test.io", "password": PW})
mallory_token, mallory_id = body.get("session_token"), body.get("user_id")
check("a second account registers on replica 2", st == 201, str(st))
st, body = call(DBW1, "/me", token=mallory_token, method="GET")
check("another tenant's token resolves to THEM, not to alice",
      st == 200 and body.get("user_id") == mallory_id and body.get("user_id") != alice_id,
      str(body))

# ── stream CRUD, scoped by the session claim ─────────────────────────────────
# The whole point of these routes: user_id comes from the token, never the request.

st, body = call(DBW1, "/streams", token=alice_token, method="GET")
check("a new account has no streams", st == 200 and body.get("streams") == [], str(body))

st, body = call(DBW1, "/streams", {"label": "north field"}, token=alice_token)
check("adding a stream returns 201 and a key", st == 201 and len(body.get("stream_key", "")) == 16,
      str(body))
alice_stream = body.get("stream_id")
alice_key = body.get("stream_key")

st, body = call(DBW2, "/streams", token=alice_token, method="GET")
check("the new stream is visible on the other replica",
      st == 200 and [s["stream_id"] for s in body["streams"]] == [alice_stream], str(body))
check("the list carries the key back (it must be retypable)",
      body["streams"][0]["stream_key"] == alice_key)

st, _ = call(DBW1, "/streams", {"label": "x" * 129}, token=alice_token)
check("an over-long label is 400", st == 400, str(st))

# ── unauthenticated and cross-tenant access ──────────────────────────────────
for path, method, label in (
        ("/streams", "GET", "list"),
        ("/streams", "POST", "add"),
        (f"/streams/{alice_stream}/rotate", "POST", "rotate"),
        (f"/streams/{alice_stream}/revoke", "POST", "revoke")):
    body_arg = {} if method == "POST" else None
    st, _ = call(DBW1, path, body_arg, method=method)
    check(f"401 without a token: {label}", st == 401, str(st))
    st, _ = call(DBW1, path, body_arg, token="garbage", method=method)
    check(f"401 with a garbage token: {label}", st == 401, str(st))

# Mallory registered earlier. Her token must not reach Alice's slot, and the
# refusal must look identical to a stream that does not exist.
st, body = call(DBW1, "/streams", token=mallory_token, method="GET")
check("another tenant's list does not include alice's stream",
      st == 200 and body.get("streams") == [], str(body))

st, m_rot = call(DBW1, f"/streams/{alice_stream}/rotate", {}, token=mallory_token)
check("another tenant cannot rotate alice's stream", m_rot != 200 and st == 404, str(st))
st, m_rev = call(DBW1, f"/streams/{alice_stream}/revoke", {}, token=mallory_token)
check("another tenant cannot revoke alice's stream", st == 404, str(st))
st, _ = call(DBW1, "/streams/999999/rotate", {}, token=mallory_token)
check("a nonexistent stream gives the SAME 404 — no enumeration", st == 404, str(st))

st, body = call(DBW1, "/streams", token=alice_token, method="GET")
check("alice's key is unchanged after the failed cross-tenant rotate",
      body["streams"][0]["stream_key"] == alice_key, str(body))

# ── rotate and revoke, as the owner ──────────────────────────────────────────
st, body = call(DBW1, f"/streams/{alice_stream}/rotate", {}, token=alice_token)
rotated_key = body.get("stream_key")
check("the owner can rotate", st == 200 and rotated_key and rotated_key != alice_key, str(body))

st, body = call(DBW2, "/streams", token=alice_token, method="GET")
check("the rotated key is what the other replica now reports",
      body["streams"][0]["stream_key"] == rotated_key, str(body))

st, _ = call(DBW1, f"/streams/{alice_stream}/revoke", {}, token=alice_token)
check("the owner can revoke", st == 200, str(st))

st, body = call(DBW1, "/streams", token=alice_token, method="GET")
check("a revoked slot is hidden by default", body.get("streams") == [], str(body))
st, body = call(DBW1, "/streams?include_revoked=true", token=alice_token, method="GET")
check("...but is still there when asked for", len(body.get("streams", [])) == 1, str(body))
check("revoking deleted nothing — the row survives with revoked_at set",
      body["streams"][0]["revoked_at"] is not None, str(body))

# ── the cap, over HTTP ───────────────────────────────────────────────────────
st, body = call(DBW1, "/register", {"email": "capped@test.io", "password": PW})
capped_token = body["session_token"]
codes = []
for i in range(11):
    st, _ = call(DBW2 if i % 2 else DBW1, "/streams", {"label": f"s{i}"}, token=capped_token)
    codes.append(st)
check("the first 10 slots are created (alternating replicas)",
      codes[:10] == [201] * 10, str(codes))
check("the 11th is 409, not 500 and not 201", codes[10] == 409, str(codes))

# The cap must hold under genuine concurrency, not just sequentially. Without the
# row lock in create_stream, N simultaneous requests all read the same "active"
# count and all insert — and there is no unique constraint to catch it afterwards
# the way there is for a duplicate email.
st, body = call(DBW1, "/register", {"email": "burst@test.io", "password": PW})
burst_token = body["session_token"]

import threading

burst_codes = []
lock = threading.Lock()


def add_one(i):
    st, _ = call(DBW2 if i % 2 else DBW1, "/streams", {"label": f"b{i}"}, token=burst_token)
    with lock:
        burst_codes.append(st)


threads = [threading.Thread(target=add_one, args=(i,)) for i in range(20)]
for t in threads:
    t.start()
for t in threads:
    t.join()

created = burst_codes.count(201)
check("20 simultaneous adds across 2 replicas create EXACTLY the cap, no more",
      created == 10, f"201s={created} all={sorted(burst_codes)}")
check("the rest are refused cleanly (409), none 500",
      burst_codes.count(409) == 10 and 500 not in burst_codes, str(sorted(burst_codes)))

st, body = call(DBW1, "/streams", token=burst_token, method="GET")
check("...and the database really holds exactly the cap",
      len(body.get("streams", [])) == 10, str(len(body.get("streams", []))))

print()
print("=" * 60)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
