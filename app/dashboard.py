"""Browser dashboard: one static page plus a JSON endpoint it reads from."""

from datetime import date, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .db import Database

PAGE = Path(__file__).parent / "static" / "dashboard.html"
RANGES = (7, 30, 90)


def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def _window(db: Database, start: date, end: date) -> dict:
    """Per-day rows and period totals for start..end inclusive."""
    lo, hi = f"{start.isoformat()}T00:00:00", f"{(end + timedelta(days=1)).isoformat()}T00:00:00"
    activities = db.list_activities(lo, hi, None, 10_000)

    days: dict[str, dict] = {}
    d = start
    while d <= end:
        days[d.isoformat()] = {"day": d.isoformat(), "training_h": {}, "sleep_h": None,
                               "sleep_score": None, "hrv_ms": None, "resting_hr": None, "steps": None}
        d += timedelta(days=1)

    for a in activities:
        row = days.get(a["start_time"][:10])
        if row is not None and a["duration_s"]:
            kind = a["activity_type"] or "other"
            row["training_h"][kind] = round(row["training_h"].get(kind, 0) + a["duration_s"] / 3600, 2)

    for h in db.get_health(start.isoformat(), end.isoformat(), None):
        row, data = days[h["day"]], h["data"]
        if h["kind"] == "sleep":
            row["sleep_h"] = round(data["duration_s"] / 3600, 2) if data.get("duration_s") else None
            row["sleep_score"] = data.get("score")
        elif h["kind"] == "hrv":
            row["hrv_ms"] = data.get("last_night_avg_ms")
        elif h["kind"] == "daily":
            row["resting_hr"] = data.get("resting_hr")
            row["steps"] = data.get("steps")

    rows = list(days.values())
    totals = {
        "activities": len(activities),
        "training_h": round(sum(a["duration_s"] or 0 for a in activities) / 3600, 1),
        "distance_km": round(sum(a["distance_m"] or 0 for a in activities) / 1000, 1),
        "sleep_h": _mean([r["sleep_h"] for r in rows]),
        "hrv_ms": _mean([r["hrv_ms"] for r in rows]),
        "resting_hr": _mean([r["resting_hr"] for r in rows]),
        "steps": _mean([r["steps"] for r in rows]),
    }
    return {"days": rows, "totals": totals, "activities": activities}


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.custom_route("/dashboard", methods=["GET"])
    async def page(_: Request) -> HTMLResponse:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"))

    @mcp.custom_route("/dashboard/api", methods=["GET"])
    async def data(request: Request) -> JSONResponse:
        try:
            n = int(request.query_params.get("days", "30"))
        except ValueError:
            n = 30
        n = n if n in RANGES else 30
        end = date.today()
        start = end - timedelta(days=n - 1)
        current = _window(db, start, end)
        previous = _window(db, start - timedelta(days=n), start - timedelta(days=1))
        return JSONResponse({
            "range": {"days": n, "from": start.isoformat(), "to": end.isoformat()},
            "days": current["days"],
            "totals": current["totals"],
            "previous_totals": previous["totals"],
            "recent_activities": current["activities"][:15],
            "sync": db.sync_overview(),
        })
