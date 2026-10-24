import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .models import Activity

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id            TEXT PRIMARY KEY,
    start_time    TEXT NOT NULL,          -- ISO 8601, local time
    activity_type TEXT,
    name          TEXT,
    duration_s    REAL,
    distance_m    REAL,
    avg_hr        REAL,
    calories      REAL,
    raw_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_time);

CREATE TABLE IF NOT EXISTS daily_health (
    day      TEXT NOT NULL,               -- YYYY-MM-DD
    kind     TEXT NOT NULL,               -- daily, sleep, hrv, body_battery, ...
    raw_json TEXT NOT NULL,
    PRIMARY KEY (day, kind)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    activities  INTEGER,
    health_days INTEGER,
    error       TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._tx() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # --- writes ---------------------------------------------------------

    def upsert_activities(self, items: list[Activity]) -> int:
        with self._tx() as c:
            c.executemany(
                """INSERT INTO activities VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     start_time=excluded.start_time, activity_type=excluded.activity_type,
                     name=excluded.name, duration_s=excluded.duration_s,
                     distance_m=excluded.distance_m, avg_hr=excluded.avg_hr,
                     calories=excluded.calories, raw_json=excluded.raw_json""",
                [
                    (a.id, a.start_time, a.activity_type, a.name, a.duration_s,
                     a.distance_m, a.avg_hr, a.calories, json.dumps(a.raw))
                    for a in items
                ],
            )
        return len(items)

    def upsert_health(self, day: str, summaries: dict[str, dict]) -> None:
        with self._tx() as c:
            c.executemany(
                """INSERT INTO daily_health VALUES (?,?,?)
                   ON CONFLICT(day, kind) DO UPDATE SET raw_json=excluded.raw_json""",
                [(day, kind, json.dumps(data)) for kind, data in summaries.items()],
            )

    def start_sync_run(self) -> int:
        with self._tx() as c:
            return c.execute("INSERT INTO sync_runs (started_at) VALUES (?)", (utcnow(),)).lastrowid

    def finish_sync_run(self, run_id: int, ok: bool, activities: int, health_days: int, error: str | None):
        with self._tx() as c:
            c.execute(
                "UPDATE sync_runs SET finished_at=?, ok=?, activities=?, health_days=?, error=? WHERE id=?",
                (utcnow(), int(ok), activities, health_days, error, run_id),
            )

    # --- reads ----------------------------------------------------------

    def list_activities(self, start: str | None, end: str | None, activity_type: str | None, limit: int) -> list[dict]:
        sql = "SELECT id, start_time, activity_type, name, duration_s, distance_m, avg_hr, calories FROM activities WHERE 1=1"
        args: list = []
        if start:
            sql += " AND start_time >= ?"
            args.append(start)
        if end:
            sql += " AND start_time < ?"
            args.append(end)
        if activity_type:
            sql += " AND activity_type = ?"
            args.append(activity_type)
        sql += " ORDER BY start_time DESC LIMIT ?"
        args.append(limit)
        with self._tx() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def get_activity(self, activity_id: str) -> dict | None:
        with self._tx() as c:
            row = c.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["raw"] = json.loads(out.pop("raw_json"))
        return out

    def get_health(self, start: str, end: str, kinds: list[str] | None) -> list[dict]:
        sql = "SELECT day, kind, raw_json FROM daily_health WHERE day >= ? AND day <= ?"
        args: list = [start, end]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        sql += " ORDER BY day, kind"
        with self._tx() as c:
            rows = c.execute(sql, args).fetchall()
        return [{"day": r["day"], "kind": r["kind"], "data": json.loads(r["raw_json"])} for r in rows]

    def activity_totals(self, start: str, end: str) -> list[dict]:
        with self._tx() as c:
            rows = c.execute(
                """SELECT activity_type, COUNT(*) AS count,
                          ROUND(SUM(duration_s) / 3600.0, 2) AS hours,
                          ROUND(SUM(distance_m) / 1000.0, 2) AS km,
                          ROUND(SUM(calories)) AS calories
                   FROM activities WHERE start_time >= ? AND start_time < ?
                   GROUP BY activity_type ORDER BY hours DESC""",
                (start, end),
            ).fetchall()
        return [dict(r) for r in rows]

    def sync_overview(self) -> dict:
        with self._tx() as c:
            last = c.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
            last_ok = c.execute("SELECT finished_at FROM sync_runs WHERE ok = 1 ORDER BY id DESC LIMIT 1").fetchone()
            counts = c.execute(
                """SELECT (SELECT COUNT(*) FROM activities) AS activities,
                          (SELECT MIN(start_time) FROM activities) AS first_activity,
                          (SELECT MAX(start_time) FROM activities) AS latest_activity,
                          (SELECT COUNT(DISTINCT day) FROM daily_health) AS health_days,
                          (SELECT MAX(day) FROM daily_health) AS latest_health_day"""
            ).fetchone()
        return {
            "last_run": dict(last) if last else None,
            "last_successful_sync": last_ok["finished_at"] if last_ok else None,
            "stored": dict(counts),
        }

    def purge_sample_data(self) -> int:
        """Remove rows written by FakeSource, so real data starts clean."""
        with self._tx() as c:
            n = c.execute("DELETE FROM activities WHERE id LIKE 'fake-%'").rowcount
            n += c.execute("DELETE FROM daily_health WHERE json_extract(raw_json, '$.source') = 'fake'").rowcount
        return n

    def is_empty(self) -> bool:
        with self._tx() as c:
            return c.execute("SELECT NOT EXISTS (SELECT 1 FROM activities) AND NOT EXISTS (SELECT 1 FROM daily_health)").fetchone()[0] == 1
