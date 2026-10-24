"""Checks the dashboard's sign-in, session cookie and data endpoint.

Run inside the container:  docker exec garmin-mcp python tests/dashboard_test.py
"""

import base64
import http.client
import json
import os
import urllib.parse

HOST, PORT = "127.0.0.1", 8000
USER = os.environ.get("DASHBOARD_USERNAME", "dan")
PW = os.environ["DASHBOARD_PASSWORD"]


def req(method: str, path: str, headers: dict | None = None, body: str | None = None):
    """One request, no redirect following. Returns (status, headers, body)."""
    c = http.client.HTTPConnection(HOST, PORT, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
    c.close()
    return out


def login(username: str, password: str):
    form = urllib.parse.urlencode({"username": username, "password": password})
    return req("POST", "/dashboard/login", {"Content-Type": "application/x-www-form-urlencoded"}, form)


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


# Signed out: page redirects to the sign-in form, API refuses, and nothing triggers a browser prompt.
s, h, _ = req("GET", "/dashboard")
check(s == 303 and h.get("location") == "/dashboard/login", f"signed-out page should redirect, got {s} {h.get('location')}")
s, h, _ = req("GET", "/dashboard/api")
check(s == 401 and "www-authenticate" not in h, f"signed-out API should be a plain 401, got {s} {h}")
s, _, body = req("GET", "/dashboard/login")
check(s == 200 and b'autocomplete="username"' in body and b'autocomplete="current-password"' in body,
      "sign-in form should be served with password-manager hints")
print("signed out: page redirects to sign-in, API 401 without a browser prompt, form has autofill hints")

# Wrong credentials. Each run makes 2 failed attempts; the lockout (5 a minute) kicks in if
# this test is run 3 times within a minute, which is also a rejection.
for u, p in ((USER, "wrong"), ("someone-else", PW)):
    s, h, _ = login(u, p)
    check(s == 303 and "set-cookie" not in h, f"bad login {u!r} was not rejected: {s} {h}")
    check(h["location"].endswith(("error=invalid", "error=locked")), f"unexpected redirect {h['location']}")
print("wrong password or username: rejected, no cookie")

# Right credentials: cookie, then the page and API work with it.
s, h, _ = login(USER, PW)
if h.get("location", "").endswith("error=locked"):
    raise SystemExit("LOCKED OUT: too many failed sign-ins from repeated test runs; wait 60 s and run again")
cookie = h.get("set-cookie", "")
check(s == 303 and h["location"] == "/dashboard" and "HttpOnly" in cookie and "SameSite=Strict" in cookie, f"login: {s} {h}")
session = cookie.split(";")[0]
s, _, body = req("GET", "/dashboard", {"Cookie": session})
check(s == 200 and b"<title>Fitness Dashboard</title>" in body and b"/dashboard/logout" in body, f"page with cookie: {s}")
for days in (7, 30, 90):
    s, _, body = req("GET", f"/dashboard/api?days={days}", {"Cookie": session})
    j = json.loads(body)
    check(s == 200 and len(j["days"]) == days, f"api {days}d with cookie: {s}")
    ins = j["insights"]
    check({"zones_min", "intensity_pct", "aerobic_te", "runs", "strength"} <= set(ins) and "previous_insights" in j,
          f"insights missing: {sorted(ins)}")
    check({"benefit_h", "zones_min", "run_km", "strength"} <= set(j["days"][0]), "per-day insight fields missing")
    pct = ins["intensity_pct"]
    if pct["easy"] is not None:
        check(97 <= pct["easy"] + pct["moderate"] + pct["hard"] <= 103, f"intensity shares don't add up: {pct}")
print("right login: HttpOnly session cookie; page and all three ranges load with it, insights included")

# A tampered cookie is refused; the cookie doesn't open the MCP endpoint.
name, value = session.split("=", 1)
expires, sig = value.split(".", 1)
flipped = sig[:-1] + ("1" if sig[-1] == "0" else "0")  # always differs from the real signature
for bad in (f"{expires}.{flipped}", f"{int(expires) + 86400 * 365}.{sig}", f"{expires}.{'0' * len(sig)}"):
    s, _, _ = req("GET", "/dashboard", {"Cookie": f"{name}={bad}"})
    check(s == 303, f"tampered cookie {bad[:12]}... should be refused, got {s}")
s, _, _ = req("POST", "/mcp", {"Cookie": session, "Content-Type": "application/json"}, "{}")
check(s == 401, f"session cookie must not open /mcp, got {s}")
print("tampered cookie refused; session cookie doesn't open /mcp")

# Behind Tailscale Funnel: lockout is per visitor (X-Forwarded-For from the proxy), the
# visitor can't choose their own address, and the cookie is Secure only over HTTPS.
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
def proxied_login(visitor_ip: str, password: str, https: bool = True, spoof: str = ""):
    headers = {**FORM, "X-Forwarded-For": f"{spoof}, {visitor_ip}" if spoof else visitor_ip}
    if https:
        headers["X-Forwarded-Proto"] = "https"
    return req("POST", "/dashboard/login", headers, urllib.parse.urlencode({"username": USER, "password": password}))

attacker, me = "203.0.113.9", "198.51.100.7"
for _ in range(5):
    proxied_login(attacker, "wrong")
s, h, _ = proxied_login(attacker, PW)
check(h["location"].endswith("error=locked"), f"attacker should be locked out, got {h['location']}")
s, h, _ = proxied_login(attacker, PW, spoof=me)
check(h["location"].endswith("error=locked"), "a spoofed X-Forwarded-For entry must not escape the lockout")
s, h, _ = proxied_login(me, PW)
check(h.get("location") == "/dashboard" and "Secure" in h.get("set-cookie", ""),
      f"another visitor must still get in, with a Secure cookie over https: {h}")
s, h, _ = proxied_login("198.51.100.8", PW, https=False)
check("Secure" not in h.get("set-cookie", "x"), "plain-HTTP sign-in must not set Secure (LAN address)")
print("behind a proxy: lockout per visitor, spoofing doesn't help, Secure cookie only over https")

# Sign out clears the cookie.
s, h, _ = req("GET", "/dashboard/logout", {"Cookie": session})
check(s == 303 and "Max-Age=0" in h.get("set-cookie", ""), f"logout: {s} {h}")
print("sign out: cookie cleared")

# Scripts can still use HTTP Basic.
basic = "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode()
s, _, _ = req("GET", "/dashboard/api?days=7", {"Authorization": basic})
check(s == 200, f"basic auth for scripts: {s}")
print("HTTP Basic still works for scripts")

# Sync freshness for outside monitors: open, and agrees with the database.
s, _, body = req("GET", "/health/sync")
j = json.loads(body)
check(s in (200, 503) and j["status"] == ("ok" if s == 200 else "stale") and "minutes_since_last_success" in j
      and "stale_after_min" in j, f"/health/sync: {s} {j}")
print(f"/health/sync: {s} {j['status']}, last success {j['minutes_since_last_success']} min ago "
      f"(stale after {j['stale_after_min']})")
print("ALL OK")
