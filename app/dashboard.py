"""Browser dashboard: one static page plus a JSON endpoint it reads from."""

from datetime import date, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import insights
from .auth import LOGIN_PATH, LOGOUT_PATH, SESSION_DAYS, Sessions
from .db import Database

PAGE = Path(__file__).parent / "static" / "dashboard.html"
LOGIN_PAGE = Path(__file__).parent / "static" / "login.html"
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

    detailed = db.activities_with_raw(lo, hi)
    insights.add_daily(days, detailed)

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
    n_days = (end - start).days + 1
    return {"days": rows, "totals": totals, "activities": activities, "insights": insights.summary(detailed, n_days)}


def register(mcp: FastMCP, db: Database, sessions: Sessions) -> None:
    @mcp.custom_route(LOGIN_PATH, methods=["GET"])
    async def login_page(_: Request) -> HTMLResponse:
        return HTMLResponse(LOGIN_PAGE.read_text(encoding="utf-8"))

    @mcp.custom_route(LOGIN_PATH, methods=["POST"])
    async def login(request: Request) -> RedirectResponse:
        client = request.client.host if request.client else "unknown"
        if sessions.locked_out(client):
            return RedirectResponse(f"{LOGIN_PATH}?error=locked", status_code=303)
        form = await request.form()
        if not sessions.check(str(form.get("username", "")), str(form.get("password", ""))):
            sessions.record_failure(client)
            return RedirectResponse(f"{LOGIN_PATH}?error=invalid", status_code=303)
        response = RedirectResponse("/dashboard", status_code=303)
        response.headers["set-cookie"] = sessions.set_cookie(sessions.issue(), SESSION_DAYS * 86400)
        return response

    @mcp.custom_route(LOGOUT_PATH, methods=["GET", "POST"])
    async def logout(_: Request) -> RedirectResponse:
        response = RedirectResponse(LOGIN_PATH, status_code=303)
        response.headers["set-cookie"] = sessions.set_cookie("", 0)
        return response

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
            "insights": current["insights"],
            "previous_insights": previous["insights"],
            "recent_activities": current["activities"][:15],
            "sync": db.sync_overview(),
        })
