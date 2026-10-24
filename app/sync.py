import logging
import threading
from datetime import date, timedelta

from .db import Database
from .sources import Source

log = logging.getLogger("garmin.sync")


class Syncer:
    """Pulls recent data from Garmin into SQLite on a timer, and on demand."""

    def __init__(self, db: Database, source: Source, interval_min: int, initial_days: int, lookback_days: int):
        self.db = db
        self.source = source
        self.interval_s = interval_min * 60
        self.initial_days = initial_days
        self.lookback_days = lookback_days
        self._run_lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="garmin-sync", daemon=True).start()

    def _loop(self) -> None:
        days = self.initial_days if self.db.is_empty() else self.lookback_days
        while not self._stop.is_set():
            self.run(days)
            days = self.lookback_days
            self._stop.wait(self.interval_s)

    def run(self, days_back: int) -> dict:
        """Sync the last `days_back` local days (today included). Skips if a sync is already running."""
        if not self._run_lock.acquire(blocking=False):
            return {"ok": False, "error": "a sync is already running"}
        run_id = self.db.start_sync_run()
        n_activities = n_days = 0
        try:
            today = date.today()
            first = today - timedelta(days=days_back - 1)

            n_activities = self.db.upsert_activities(self.source.fetch_activities(first, today))
            for day, summaries in sorted(self.source.fetch_health(first, today).items()):
                if summaries:
                    self.db.upsert_health(day, summaries)
                    n_days += 1

            self.db.finish_sync_run(run_id, True, n_activities, n_days, None)
            log.info("sync ok: %d activities, %d health days", n_activities, n_days)
            return {"ok": True, "activities": n_activities, "health_days": n_days, "from": first.isoformat()}
        except Exception as e:
            self.db.finish_sync_run(run_id, False, n_activities, n_days, str(e))
            log.exception("sync failed")
            return {"ok": False, "error": str(e)}
        finally:
            self._run_lock.release()
