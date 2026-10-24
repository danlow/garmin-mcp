"""Training analysis for the dashboard, from the full Garmin activity summaries.

Uses Garmin's own per-activity fields: trainingEffectLabel (primary benefit), aerobic and
anaerobic training effect, hrTimeInZone_1..5, splitSummaries, fastestSplit_*, and strength
summarizedExerciseSets.
"""

from collections import defaultdict

# Garmin's primary-benefit labels, grouped into five intensity levels (low to high).
BENEFIT_GROUPS = {
    "RECOVERY": "recovery",
    "AEROBIC_BASE": "base",
    "TEMPO": "tempo", "LACTATE_THRESHOLD": "tempo", "THRESHOLD": "tempo",
    "VO2MAX": "vo2max",
    "ANAEROBIC_CAPACITY": "anaerobic", "SPEED": "anaerobic", "SPRINT": "anaerobic",
}
QUALITY_BENEFITS = {"tempo", "vo2max", "anaerobic"}
LONG_RUN_KM = 10
LONG_RUN_MIN = 75


def benefit_group(raw: dict) -> str:
    return BENEFIT_GROUPS.get(raw.get("trainingEffectLabel") or "", "unrated")


def is_run(activity_type: str | None) -> bool:
    return "run" in (activity_type or "")


def is_strength(activity_type: str | None) -> bool:
    return "strength" in (activity_type or "")


def run_kind(a: dict) -> str:
    """quality (intervals or a hard primary benefit), long (10 km+ or 75 min+), otherwise easy."""
    raw = a["raw"]
    has_intervals = any(s.get("splitType") == "INTERVAL_ACTIVE" for s in raw.get("splitSummaries") or [])
    if has_intervals or benefit_group(raw) in QUALITY_BENEFITS:
        return "quality"
    if (a["distance_m"] or 0) >= LONG_RUN_KM * 1000 or (a["duration_s"] or 0) >= LONG_RUN_MIN * 60:
        return "long"
    return "easy"


def _add(d: dict, key: str, value: float, digits: int = 2) -> None:
    d[key] = round(d.get(key, 0) + value, digits)


def add_daily(days: dict[str, dict], activities: list[dict]) -> None:
    """Add per-day insight fields to the dashboard's day rows (keyed by YYYY-MM-DD)."""
    for row in days.values():
        row.update({"benefit_h": {}, "zones_min": [0, 0, 0, 0, 0], "run_km": {},
                    "strength": {"sessions": 0, "sets": 0, "reps": 0}})
    for a in activities:
        row = days.get(a["start_time"][:10])
        if row is None:
            continue
        raw = a["raw"]
        if a["duration_s"]:
            _add(row["benefit_h"], benefit_group(raw), a["duration_s"] / 3600)
        for i in range(5):
            row["zones_min"][i] = round(row["zones_min"][i] + (raw.get(f"hrTimeInZone_{i + 1}") or 0) / 60, 1)
        if is_run(a["activity_type"]):
            _add(row["run_km"], run_kind(a), (a["distance_m"] or 0) / 1000)
        if is_strength(a["activity_type"]):
            s = row["strength"]
            s["sessions"] += 1
            s["sets"] += raw.get("activeSets") or raw.get("totalSets") or 0
            s["reps"] += raw.get("totalReps") or 0


def summary(activities: list[dict], n_days: int) -> dict:
    """Period-level insight numbers for the tiles and panels."""
    zones = [sum(a["raw"].get(f"hrTimeInZone_{i}") or 0 for a in activities) / 60 for i in range(1, 6)]
    zone_total = sum(zones)
    pct = lambda v: round(100 * v / zone_total) if zone_total else None
    aerobic = [a["raw"]["aerobicTrainingEffect"] for a in activities if a["raw"].get("aerobicTrainingEffect")]
    anaerobic = [a["raw"].get("anaerobicTrainingEffect") or 0 for a in activities if a["raw"].get("aerobicTrainingEffect")]
    avg = lambda v: round(sum(v) / len(v), 1) if v else None

    runs = [a for a in activities if is_run(a["activity_type"])]
    kinds = defaultdict(int)
    for r in runs:
        kinds[run_kind(r)] += 1
    best = lambda field: min((r["raw"][field] for r in runs if r["raw"].get(field)), default=None)

    strength = [a for a in activities if is_strength(a["activity_type"])]
    exercises: dict[str, dict] = defaultdict(lambda: {"sets": 0, "reps": 0})
    for s in strength:
        for ex in s["raw"].get("summarizedExerciseSets") or []:
            name = ex.get("category") or "UNKNOWN"
            exercises[name]["sets"] += ex.get("sets") or 0
            exercises[name]["reps"] += ex.get("reps") or 0
    top = sorted(({"exercise": k, **v} for k, v in exercises.items()), key=lambda e: -e["sets"])[:8]

    weeks = n_days / 7
    return {
        "zones_min": [round(z) for z in zones],
        "intensity_pct": {"easy": pct(zones[0] + zones[1]), "moderate": pct(zones[2]), "hard": pct(zones[3] + zones[4])},
        "aerobic_te": avg(aerobic),
        "anaerobic_te": avg(anaerobic),
        "runs": {
            "count": len(runs),
            "per_week": round(len(runs) / weeks, 1),
            "km": round(sum(r["distance_m"] or 0 for r in runs) / 1000, 1),
            "longest_km": round(max((r["distance_m"] or 0 for r in runs), default=0) / 1000, 1),
            "easy": kinds["easy"], "long": kinds["long"], "quality": kinds["quality"],
            "best_1k_s": best("fastestSplit_1000"),
            "best_5k_s": best("fastestSplit_5000"),
            "best_10k_s": best("fastestSplit_10000"),
        },
        "strength": {
            "sessions": len(strength),
            "per_week": round(len(strength) / weeks, 1),
            "sets": sum(s["raw"].get("activeSets") or s["raw"].get("totalSets") or 0 for s in strength),
            "reps": sum(s["raw"].get("totalReps") or 0 for s in strength),
            "top_exercises": top,
        },
    }
