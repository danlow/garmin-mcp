from dataclasses import dataclass, field


@dataclass
class Activity:
    id: str
    start_time: str  # ISO 8601, local time of the activity, no offset
    activity_type: str
    name: str
    duration_s: float | None = None
    distance_m: float | None = None
    avg_hr: float | None = None
    calories: float | None = None
    raw: dict = field(default_factory=dict)  # full summary as Garmin returned it
