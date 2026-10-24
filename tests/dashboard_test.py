"""Checks the dashboard's auth and data endpoint.

Run inside the container:  docker exec garmin-mcp python tests/dashboard_test.py
"""

import base64
import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")


def get(path: str, password: str | None = None):
    req = urllib.request.Request(BASE + path)
    if password is not None:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"dan:{password}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


pw = os.environ["DASHBOARD_PASSWORD"]
for path in ("/dashboard", "/dashboard/api"):
    assert get(path)[0] == 401, f"{path} open without a password"
    assert get(path, "wrong")[0] == 401, f"{path} accepted a wrong password"
print("auth: rejects missing and wrong passwords")

status, body = get("/dashboard", pw)
assert status == 200 and b"<title>Fitness Dashboard</title>" in body, status
print("page: 200,", len(body), "bytes")

for days in (7, 30, 90):
    status, body = get(f"/dashboard/api?days={days}", pw)
    assert status == 200, status
    j = json.loads(body)
    assert len(j["days"]) == days, len(j["days"])
    print(f"api {days}d:", j["range"]["from"], "to", j["range"]["to"], "| totals", json.dumps(j["totals"]),
          "| previous activities", j["previous_totals"]["activities"])
print("ALL OK")
