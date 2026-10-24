import logging
from datetime import date, timedelta
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import dashboard
from .auth import Auth, Sessions
from .config import Config
from .db import Database
from .sources import make_source
from .sync import Syncer

HEALTH_KINDS = ["daily", "sleep", "hrv", "body_battery", "stress"]


def _day(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"dates must be YYYY-MM-DD, got {value!r}")


def _day_range(start_date: str | None, end_date: str | None, default_days: int) -> tuple[date, date]:
    end = _day(end_date, date.today())
    start = _day(start_date, end - timedelta(days=default_days - 1))
    if start > end:
        raise ValueError("start_date is after end_date")
    return start, end


def _iso_bounds(start: date, end: date) -> tuple[str, str]:
    """Half-open local timestamp range covering whole days start..end."""
    return f"{start.isoformat()}T00:00:00", f"{(end + timedelta(days=1)).isoformat()}T00:00:00"


def build_server(db: Database, syncer: Syncer, sessions: Sessions) -> FastMCP:
    mcp = FastMCP(
        "garmin",
        instructions=(
            "Personal Garmin Connect data (activities and daily health), synced into a local "
            "database on a schedule. Dates are YYYY-MM-DD and times are the user's local time. "
            "Distances are metres, durations seconds, unless a tool says otherwise. "
            "Call sync_status if data looks stale."
        ),
        host="0.0.0.0",
        stateless_http=True,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    dashboard.register(mcp, db, sessions)

    @mcp.tool()
    def list_activities(
        start_date: str | None = None,
        end_date: str | None = None,
        activity_type: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """List activities, newest first. Defaults to the last 30 days.

        activity_type filters exactly (e.g. running, cycling, swimming, strength_training).
        """
        start, end = _day_range(start_date, end_date, 30)
        lo, hi = _iso_bounds(start, end)
        return db.list_activities(lo, hi, activity_type, max(1, min(limit, 200)))

    @mcp.tool()
    def get_activity(activity_id: str) -> dict[str, Any]:
        """Full stored summary for one activity, including every field Garmin returned."""
        found = db.get_activity(activity_id)
        if found is None:
            raise ValueError(f"no activity with id {activity_id!r}")
        return found

    @mcp.tool()
    def get_health(
        start_date: str | None = None,
        end_date: str | None = None,
        kinds: list[str] | None = None,
    ) -> list[dict]:
        """Daily health summaries, oldest first. Defaults to the last 7 days.

        kinds: any of daily (steps, resting HR, calories), sleep, hrv, body_battery, stress.
        Omit to get all of them.
        """
        if kinds:
            unknown = sorted(set(kinds) - set(HEALTH_KINDS))
            if unknown:
                raise ValueError(f"unknown kinds {unknown}; valid: {HEALTH_KINDS}")
        start, end = _day_range(start_date, end_date, 7)
        return db.get_health(start.isoformat(), end.isoformat(), kinds)

    @mcp.tool()
    def training_totals(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        """Totals per activity type (count, hours, km, calories). Defaults to the last 7 days."""
        start, end = _day_range(start_date, end_date, 7)
        lo, hi = _iso_bounds(start, end)
        return {"from": start.isoformat(), "to": end.isoformat(), "by_type": db.activity_totals(lo, hi)}

    @mcp.tool()
    def sync_status() -> dict[str, Any]:
        """When data was last synced from Garmin, whether it worked, and how much is stored."""
        return db.sync_overview()

    @mcp.tool()
    def sync_now(days_back: int = 3) -> dict[str, Any]:
        """Pull the last `days_back` days (1-90) from Garmin right away instead of waiting for the schedule."""
        return syncer.run(max(1, min(days_back, 90)))

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config.from_env()
    db = Database(cfg.db_path)
    source = make_source(cfg.source, cfg.garmin_api_key)
    if cfg.source != "fake" and (n := db.purge_sample_data()):
        logging.info("removed %d sample-data rows", n)
    syncer = Syncer(db, source, cfg.sync_interval_min, cfg.initial_days, cfg.lookback_days)
    syncer.start()
    sessions = Sessions(cfg.dashboard_username, cfg.dashboard_password, cfg.token)
    app = Auth(build_server(db, syncer, sessions).streamable_http_app(), cfg.token, sessions)
    uvicorn.run(app, host="0.0.0.0", port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
