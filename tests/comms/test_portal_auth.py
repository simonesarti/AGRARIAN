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

print()
print("=" * 60)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
