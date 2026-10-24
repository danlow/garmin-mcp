import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    token: str
    dashboard_username: str
    dashboard_password: str  # empty = dashboard locked
    db_path: str
    port: int
    source: str  # "fake" sample data or "garmin"
    garmin_api_key: str
    sync_interval_min: int
    initial_days: int
    lookback_days: int
    stale_after_min: int  # 0 = 3 x sync interval

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("MCP_TOKEN", "").strip()
        if len(token) < 24:
            raise SystemExit("MCP_TOKEN must be set to a random string of at least 24 characters")
        return cls(
            token=token,
            dashboard_username=os.environ.get("DASHBOARD_USERNAME", "dan").strip(),
            dashboard_password=os.environ.get("DASHBOARD_PASSWORD", "").strip(),
            db_path=os.environ.get("DB_PATH", "/data/garmin.db"),
            port=int(os.environ.get("PORT", "8000")),
            source=os.environ.get("GARMIN_SOURCE", "fake").lower(),
            garmin_api_key=os.environ.get("GARMIN_API_KEY", "").strip(),
            sync_interval_min=int(os.environ.get("SYNC_INTERVAL_MIN", "60")),
            initial_days=int(os.environ.get("INITIAL_SYNC_DAYS", "180")),
            lookback_days=int(os.environ.get("LOOKBACK_DAYS", "3")),
            stale_after_min=int(os.environ.get("SYNC_STALE_MIN") or 0),
        )
