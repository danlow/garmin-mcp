"""GarminApiSource against a simulated Personal API (shapes from Garmin's Swagger docs).

Run inside the container:  docker exec garmin-mcp python -m tests.garmin_client_test
"""

from datetime import date, timedelta

import httpx

from app.sources import GarminApiSource

calls: list[tuple[str, dict]] = []
state = {"tokens_issued": 0, "reject_next": False}


def handler(req: httpx.Request) -> httpx.Response:
    path, q = req.url.path, dict(req.url.params)
    calls.append((path, q))
    auth = req.headers.get("authorization", "")

    if path.endswith("/personal-api-key/token"):
        if auth != "ApiKey test-key":
            return httpx.Response(401)
        state["tokens_issued"] += 1
        return httpx.Response(200, json={"access_token": f"tok{state['tokens_issued']}", "expires_in": 86400})

    if not auth.startswith("Bearer tok"):
        return httpx.Response(401)
    if state["reject_next"]:  # simulate a token revoked early
        state["reject_next"] = False
        return httpx.Response(401)

    if path == "/activitylist-service/activities/search/activities":
        start, limit = int(q["start"]), int(q["limit"])
        total = 130  # forces a second page at limit 99
        return httpx.Response(200, json=[{
            "activityId": 1000 + i, "activityName": f"Run {i}", "startTimeLocal": "2026-10-20 07:05:00",
            "startTimeGMT": "2026-10-19 23:05:00", "activityType": {"typeKey": "trail_running", "typeId": 6},
            "duration": 3600.5, "distance": 10000.0, "averageHR": 150.0, "calories": 700.0,
        } for i in range(start, min(start + limit, total))])

    if path == "/usersummary-service/v2/dailysummaries":
        return httpx.Response(200, json={"data": [{
            "calendarDate": q["startDate"],
            "movement": {"steps": {"value": 12345, "goal": 10000, "distanceInMeters": 9000.0}},
            "heartRate": {"restingValue": 51, "minValue": 45, "maxValue": 170},
            "calories": {"burnedActive": 800, "burnedTotal": 2600},
            "stress": {"avgLevel": 30, "maxLevel": 88, "qualifier": "BALANCED"},
            "bodyBattery": {"chargedValue": 60, "drainedValue": 55, "maxValue": 95, "minValue": 20, "endOfDayValue": 40},
        }]})

    if path == "/sleep-service/sleep/dailySleepSummariesByDate":
        return httpx.Response(200, json=[
            {"calendarDate": q["startDate"], "sleepTimeSeconds": 27000, "deepSleepSeconds": 5000,
             "remSleepSeconds": 6000, "lightSleepSeconds": 14000, "awakeSleepSeconds": 2000,
             "sleepScores": {"overall": {"value": 82, "qualifierKey": "GOOD"}}, "avgHeartRate": 52.0},
            {"calendarDate": q["endDate"], "sleepTimeSeconds": None},  # no sleep recorded: skipped
        ])

    if path.startswith("/hrv-service/hrv/daily/"):
        frm = path.split("/")[-2]
        return httpx.Response(200, json={"hrvSummaries": [{
            "calendarDate": frm, "lastNightAvg": 58, "weeklyAvg": 55, "status": "BALANCED",
            "baseline": {"balancedLow": 48, "balancedUpper": 66},
        }]})

    return httpx.Response(404)


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


src = GarminApiSource("test-key", transport=httpx.MockTransport(handler))

# Activities: paging and mapping.
acts = src.fetch_activities(date(2026, 10, 1), date(2026, 10, 24))
check(len(acts) == 130, f"expected 130 activities over 2 pages, got {len(acts)}")
a = acts[0]
check((a.id, a.start_time, a.activity_type, a.duration_s, a.distance_m, a.avg_hr) ==
      ("1000", "2026-10-20T07:05:00", "trail_running", 3600.5, 10000.0, 150.0), f"bad mapping {a}")
check(a.raw["activityName"] == "Run 0", "raw summary not kept")
pages = [q for p, q in calls if p.endswith("/search/activities")]
check([q["start"] for q in pages] == ["0", "99"], f"paging offsets {pages}")
check(pages[0]["startDate"] == "2026-10-01" and pages[0]["endDate"] == "2026-10-24", "date filter not sent")
print("activities: 130 over 2 pages, local start time, type and raw kept")

# Health over 180 days: ranges split to each endpoint's limit.
calls.clear()
last = date(2026, 10, 24)
first = last - timedelta(days=179)
health = src.fetch_health(first, last)
by = lambda prefix: [q or p for p, q in calls if p.startswith(prefix)]
check(len(by("/usersummary-service/v2/dailysummaries")) == 1, "daily summaries should be 1 call (<=365 d)")
check(len(by("/sleep-service/")) == 6, f"sleep should be 6 calls of <=31 d, got {len(by('/sleep-service/'))}")
check(len(by("/hrv-service/")) == 7, f"hrv should be 7 calls of <=28 d, got {len(by('/hrv-service/'))}")
day = first.isoformat()
check(health[day]["daily"] == {"steps": 12345, "steps_goal": 10000, "distance_m": 9000.0, "resting_hr": 51,
                               "min_hr": 45, "max_hr": 170, "active_calories": 800, "total_calories": 2600},
      f"daily {health[day].get('daily')}")
check(health[day]["stress"] == {"avg": 30, "max": 88, "qualifier": "BALANCED"}, "stress mapping")
check(health[day]["body_battery"]["highest"] == 95 and health[day]["body_battery"]["end_of_day"] == 40, "body battery")
check(health[day]["sleep"]["duration_s"] == 27000 and health[day]["sleep"]["score"] == 82, "sleep mapping")
check(health[day]["hrv"]["last_night_avg_ms"] == 58 and health[day]["hrv"]["baseline_high_ms"] == 66, "hrv mapping")
sleep_days = [d for d, kinds in health.items() if "sleep" in kinds]
check(len(sleep_days) == 6, f"nights without sleep must be skipped, got {len(sleep_days)} sleep days")
print("health: 1 daily + 6 sleep + 7 hrv calls for 180 days; all fields mapped; empty nights skipped")

# Token: reused across calls, renewed once when Garmin rejects it.
check(state["tokens_issued"] == 1, f"token should be reused, issued {state['tokens_issued']}")
state["reject_next"] = True
src.fetch_activities(last, last)
check(state["tokens_issued"] == 2, "a rejected token should trigger one renewal")
print("token: one token reused for all calls; renewed after a 401")

# Bad key: clear error that doesn't contain the key.
try:
    GarminApiSource("wrong-key", transport=httpx.MockTransport(handler)).fetch_activities(last, last)
    check(False, "a wrong key should fail")
except RuntimeError as e:
    check("rejected the API key" in str(e) and "wrong-key" not in str(e), f"bad-key message: {e}")
    print("bad key:", e)

print("ALL OK")
