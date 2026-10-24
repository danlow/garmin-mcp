"""One-off check against the real Garmin API: token, then the last 7 days. Prints counts and
field names only, never the key or the token.

Run without touching the live server:
  docker compose run --rm --no-deps garmin-mcp python -m tests.live_check
"""

import os
from datetime import date, timedelta

from app.sources import GarminApiSource

src = GarminApiSource(os.environ.get("GARMIN_API_KEY", "").strip())

r = src._request_token()
ctype = r.headers.get("content-type", "")
shape = sorted(r.json()) if "json" in ctype and isinstance(r.json(), dict) else f"{type(r.json() if 'json' in ctype else r.text).__name__}, {len(r.text)} chars"
print(f"token endpoint: HTTP {r.status_code}, {ctype or 'no content-type'}, body: {shape}")
if r.status_code != 200:
    raise SystemExit(f"stopping: {r.text[:200]}")
token, ttl = src._parse_token(r)
print(f"token parsed: {len(token)} chars, valid ~{ttl // 3600} h")

last = date.today()
first = last - timedelta(days=6)
acts = src.fetch_activities(first, last)
print(f"activities {first}..{last}: {len(acts)}")
for a in acts[:5]:
    print(f"  {a.start_time}  {a.activity_type:<20} {a.duration_s and round(a.duration_s / 60)} min  "
          f"{a.distance_m and round(a.distance_m / 1000, 1)} km  HR {a.avg_hr}")

health = src.fetch_health(first, last)
for day in sorted(health):
    kinds = health[day]
    print(f"  {day}: " + ", ".join(f"{k}({len(v)} fields)" for k, v in sorted(kinds.items())))
print("days with health data:", len(health))
