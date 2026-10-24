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

# Wrong credentials.
for u, p in ((USER, "wrong"), ("someone-else", PW)):
    s, h, _ = login(u, p)
    check(s == 303 and h["location"].endswith("error=invalid") and "set-cookie" not in h, f"bad login {u!r} accepted")
print("wrong password or username: rejected, no cookie")

# Right credentials: cookie, then the page and API work with it.
s, h, _ = login(USER, PW)
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
s, _, _ = req("GET", "/dashboard", {"Cookie": f"{name}={value[:-1]}0"})
check(s == 303, f"tampered cookie should be refused, got {s}")
s, _, _ = req("POST", "/mcp", {"Cookie": session, "Content-Type": "application/json"}, "{}")
check(s == 401, f"session cookie must not open /mcp, got {s}")
print("tampered cookie refused; session cookie doesn't open /mcp")

# Sign out clears the cookie.
s, h, _ = req("GET", "/dashboard/logout", {"Cookie": session})
check(s == 303 and "Max-Age=0" in h.get("set-cookie", ""), f"logout: {s} {h}")
print("sign out: cookie cleared")

# Scripts can still use HTTP Basic.
basic = "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode()
s, _, _ = req("GET", "/dashboard/api?days=7", {"Authorization": basic})
check(s == 200, f"basic auth for scripts: {s}")
print("HTTP Basic still works for scripts")
print("ALL OK")
