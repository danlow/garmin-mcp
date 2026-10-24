"""Where data comes from. Everything else in the app only talks to the `Source` interface."""

import logging
import random
import threading
import time as clock
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterator, Protocol

import httpx

from .models import Activity

log = logging.getLogger("garmin.source")

# Health summaries for one day, keyed by kind: daily, sleep, hrv, body_battery, stress.
DayHealth = dict[str, dict]


class Source(Protocol):
    def fetch_activities(self, first: date, last: date) -> list[Activity]:
        """Activities whose local start date falls in first..last inclusive."""

    def fetch_health(self, first: date, last: date) -> dict[str, DayHealth]:
        """Health summaries per local day ("YYYY-MM-DD") in first..last inclusive.
        Days and kinds Garmin has no data for are left out."""


def _chunks(first: date, last: date, max_days: int) -> Iterator[tuple[str, str]]:
    """Split first..last into inclusive ranges of at most max_days (Garmin caps range lengths)."""
    while first <= last:
        end = min(last, first + timedelta(days=max_days - 1))
        yield first.isoformat(), end.isoformat()
        first = end + timedelta(days=1)


def _local_time(value) -> str | None:
    """Garmin's *Local timestamps arrive as epoch ms of the local wall-clock time; render them as ISO."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return value


def _round(value, digits: int = 1):
    return round(value, digits) if isinstance(value, float) else value


def _clean(d: dict) -> dict:
    """Drop missing values; an all-empty summary becomes {} so it isn't stored."""
    return {k: v for k, v in d.items() if v is not None}


class GarminApiSource:
    """Garmin Connect Personal API (https://personal.connectapi.garmin.com).

    Auth: the personal API key is exchanged for an access token (valid 24 h, no refresh
    tokens) at the token endpoint with `Authorization: ApiKey <key>`; data calls send
    `Authorization: Bearer <token>`. A new token is fetched when the old one expires or is
    rejected.
    """

    BASE_URL = "https://personal.connectapi.garmin.com"
    TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
    TOKEN_TTL_S = 24 * 3600
    PAGE_SIZE = 99

    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None):
        if not api_key:
            raise SystemExit("GARMIN_SOURCE=garmin needs GARMIN_API_KEY set in .env")
        self._api_key = api_key
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = threading.Lock()
        self._http = httpx.Client(base_url=self.BASE_URL, timeout=30, transport=transport,
                                  headers={"Accept": "application/json"})

    # --- auth -----------------------------------------------------------

    def _access_token(self) -> str:
        with self._token_lock:
            if self._token and clock.time() < self._token_expires - 600:
                return self._token
            r = self._request_token()
            if r.status_code in (401, 403):
                raise RuntimeError(
                    f"Garmin rejected the API key (HTTP {r.status_code}). It may be mistyped, deleted or "
                    "expired; create a new one on Garmin Connect Web and update GARMIN_API_KEY.")
            r.raise_for_status()
            self._token, ttl = self._parse_token(r)
            self._token_expires = clock.time() + ttl
            log.info("got a new Garmin access token, valid ~%d h", ttl // 3600)
            return self._token

    def _request_token(self) -> httpx.Response:
        # The token endpoint answers 406 to "Accept: application/json", so accept anything here.
        return self._http.post(self.TOKEN_PATH, headers={"Authorization": f"ApiKey {self._api_key}", "Accept": "*/*"})

    def _parse_token(self, r: httpx.Response) -> tuple[str, int]:
        # The docs don't show the response body, so accept the usual shapes.
        if "json" not in r.headers.get("content-type", ""):
            token = r.text.strip().strip('"')
            if token:
                return token, self.TOKEN_TTL_S
            raise RuntimeError("Garmin's token endpoint returned an empty response")
        body = r.json()
        if isinstance(body, str) and body:
            return body, self.TOKEN_TTL_S
        if isinstance(body, dict):
            for key in ("access_token", "accessToken", "token"):
                if isinstance(body.get(key), str) and body[key]:
                    ttl = body.get("expires_in") or body.get("expiresIn")
                    return body[key], int(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else self.TOKEN_TTL_S
            # Field names only, never values: this may hold the token under a name we don't know.
            raise RuntimeError(f"unrecognised token response from Garmin, fields: {sorted(body)}")
        raise RuntimeError(f"unrecognised token response from Garmin ({type(body).__name__})")

    def _get(self, path: str, params: dict | None = None):
        for attempt in (1, 2):
            r = self._http.get(path, params=params, headers={"Authorization": f"Bearer {self._access_token()}"})
            if r.status_code == 401 and attempt == 1:
                self._token = None  # expired or revoked early: get a fresh one and retry once
                continue
            if r.status_code == 429:
                raise RuntimeError(f"Garmin rate limit hit on {path}; will retry on the next scheduled sync")
            if r.status_code == 204:
                return None
            if r.status_code >= 400:
                raise RuntimeError(f"Garmin {path} answered HTTP {r.status_code}: {r.text[:200]}")
            return r.json() if r.content else None
        raise RuntimeError(f"Garmin keeps rejecting the access token on {path}")

    # --- activities -----------------------------------------------------

    def fetch_activities(self, first: date, last: date) -> list[Activity]:
        out: list[Activity] = []
        offset = 0
        while True:
            page = self._get("/activitylist-service/activities/search/activities", {
                "startDate": first.isoformat(), "endDate": last.isoformat(),
                "start": offset, "limit": self.PAGE_SIZE, "sortOrder": "asc",
            }) or []
            out.extend(self._activity(a) for a in page if a.get("activityId") is not None)
            if len(page) < self.PAGE_SIZE:
                return out
            offset += self.PAGE_SIZE

    @staticmethod
    def _activity(a: dict) -> Activity:
        started = (a.get("startTimeLocal") or a.get("startTimeGMT") or "").replace(" ", "T")[:19]
        return Activity(
            id=str(a["activityId"]),
            start_time=started,
            activity_type=(a.get("activityType") or {}).get("typeKey") or "other",
            name=a.get("activityName") or "",
            duration_s=_round(a.get("duration")),
            distance_m=_round(a.get("distance")) or None,
            avg_hr=a.get("averageHR"),
            calories=a.get("calories"),
            raw=a,
        )

    # --- health ---------------------------------------------------------

    def fetch_health(self, first: date, last: date) -> dict[str, DayHealth]:
        out: dict[str, DayHealth] = defaultdict(dict)

        def put(day: str | None, kind: str, summary: dict) -> None:
            summary = _clean(summary)
            if day and summary:
                out[day[:10]][kind] = summary

        for a, b in _chunks(first, last, 365):  # daily summaries: up to 1 year per call
            body = self._get("/usersummary-service/v2/dailysummaries", {"startDate": a, "endDate": b}) or {}
            for d in body.get("data") or []:
                day = d.get("calendarDate")
                hr, cal = d.get("heartRate") or {}, d.get("calories") or {}
                steps = (d.get("movement") or {}).get("steps") or {}
                put(day, "daily", {
                    "steps": steps.get("value"), "steps_goal": steps.get("goal"),
                    "distance_m": steps.get("distanceInMeters"),
                    "resting_hr": hr.get("restingValue"), "min_hr": hr.get("minValue"), "max_hr": hr.get("maxValue"),
                    "active_calories": cal.get("burnedActive"), "total_calories": cal.get("burnedTotal"),
                })
                s = d.get("stress") or {}
                put(day, "stress", {"avg": s.get("avgLevel"), "max": s.get("maxLevel"), "qualifier": s.get("qualifier")})
                bb = d.get("bodyBattery") or {}
                put(day, "body_battery", {
                    "charged": bb.get("chargedValue"), "drained": bb.get("drainedValue"),
                    "highest": bb.get("maxValue"), "lowest": bb.get("minValue"), "end_of_day": bb.get("endOfDayValue"),
                })

        for a, b in _chunks(first, last, 31):  # sleep: max 31 days per call
            for s in self._get("/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": a, "endDate": b}) or []:
                if not s.get("sleepTimeSeconds"):
                    continue
                put(s.get("calendarDate"), "sleep", {
                    "duration_s": s.get("sleepTimeSeconds"), "deep_s": s.get("deepSleepSeconds"),
                    "light_s": s.get("lightSleepSeconds"), "rem_s": s.get("remSleepSeconds"),
                    "awake_s": s.get("awakeSleepSeconds"),
                    "score": ((s.get("sleepScores") or {}).get("overall") or {}).get("value"),
                    "avg_hr": s.get("avgHeartRate"), "avg_stress": s.get("avgSleepStress"),
                    "start_local": _local_time(s.get("sleepStartTimestampLocal")),
                    "end_local": _local_time(s.get("sleepEndTimestampLocal")),
                })

        for a, b in _chunks(first, last, 28):  # HRV: range limit undocumented, stay conservative
            body = self._get(f"/hrv-service/hrv/daily/{a}/{b}") or {}
            for h in body.get("hrvSummaries") or []:
                base = h.get("baseline") or {}
                put(h.get("calendarDate"), "hrv", {
                    "last_night_avg_ms": h.get("lastNightAvg"), "weekly_avg_ms": h.get("weeklyAvg"),
                    "status": h.get("status"),
                    "baseline_low_ms": base.get("balancedLow"), "baseline_high_ms": base.get("balancedUpper"),
                })

        return dict(out)


class FakeSource:
    """Plausible, repeatable sample data so the server can be tested end to end without Garmin."""

    TYPES = [("running", "Morning Run"), ("cycling", "Evening Ride"),
             ("strength_training", "Strength"), ("swimming", "Pool Swim")]

    def fetch_activities(self, first: date, last: date) -> list[Activity]:
        out = []
        day = first
        while day <= last:
            rng = random.Random(f"act-{day}")
            if rng.random() < 0.7:
                kind, name = rng.choice(self.TYPES)
                started = datetime.combine(day, time(rng.randint(6, 19), rng.choice([0, 15, 30, 45])))
                duration = rng.randint(25, 90) * 60
                speed = {"running": 2.9, "cycling": 7.5, "swimming": 0.8}.get(kind)  # m/s
                act = Activity(
                    id=f"fake-{day.isoformat()}",
                    start_time=started.isoformat(timespec="seconds"),
                    activity_type=kind,
                    name=name,
                    duration_s=duration,
                    distance_m=round(duration * speed * rng.uniform(0.9, 1.1)) if speed else None,
                    avg_hr=rng.randint(118, 162),
                    calories=round(duration / 60 * rng.uniform(7, 12)),
                )
                act.raw = {k: v for k, v in act.__dict__.items() if k != "raw"} | {"source": "fake"}
                out.append(act)
            day += timedelta(days=1)
        return out

    def fetch_health(self, first: date, last: date) -> dict[str, DayHealth]:
        out = {}
        day = first
        while day <= last:
            rng = random.Random(f"health-{day}")
            sleep_s = rng.randint(6 * 3600, 8 * 3600 + 1800)
            out[day.isoformat()] = {
                "daily": {"steps": rng.randint(4000, 16000), "resting_hr": rng.randint(48, 58),
                          "active_calories": rng.randint(300, 1100), "source": "fake"},
                "sleep": {"duration_s": sleep_s, "deep_s": int(sleep_s * rng.uniform(0.12, 0.22)),
                          "rem_s": int(sleep_s * rng.uniform(0.18, 0.25)), "score": rng.randint(60, 92), "source": "fake"},
                "hrv": {"last_night_avg_ms": rng.randint(38, 72), "status": rng.choice(["BALANCED", "BALANCED", "UNBALANCED"]),
                        "source": "fake"},
                "body_battery": {"charged": rng.randint(30, 75), "drained": rng.randint(35, 80),
                                 "highest": rng.randint(70, 100), "lowest": rng.randint(5, 30), "source": "fake"},
                "stress": {"avg": rng.randint(20, 45), "max": rng.randint(60, 95), "source": "fake"},
            }
            day += timedelta(days=1)
        return out


def make_source(name: str, api_key: str = "") -> Source:
    if name == "fake":
        return FakeSource()
    if name == "garmin":
        return GarminApiSource(api_key)
    raise SystemExit(f"GARMIN_SOURCE must be 'fake' or 'garmin', got {name!r}")
