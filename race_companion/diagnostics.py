"""Inspect available feed fields and missing metadata without guessing them."""

from collections import Counter
import json

from .feed import replay_records
from .state import TimingStore


def inspect_recording(path):
    types = Counter()
    fields = {}
    store = TimingStore()
    count = 0
    now = 0
    for record in replay_records(path):
        count += 1
        now = record["received_elapsed_s"]
        store.consume(record)
        if record["kind"] == "message":
            try:
                message = json.loads(record["raw"])
                if not isinstance(message, dict):
                    continue
                kind = str(message.get("type"))
                types[kind] += 1
                fields.setdefault(kind, set()).update(message)
            except (ValueError, TypeError):
                continue
    snapshot = store.snapshot(now)
    return {
        "records": count, "recording_duration_s": now,
        "cars": len(store.cars), "message_types": dict(types),
        "fields_by_type": {key: sorted(value) for key, value in fields.items()},
        "latest_session_time_s": snapshot["latest_session_time_s"],
        "missing_categories": [car["number"] for car in snapshot["cars"] if not car["category"]],
        "missing_category_explanation": "No car metadata messages in capture" if not types["car"] else "Inspect category values or supply an explicit metadata file",
        "completed_lap_range": [min((car.lap for car in store.cars.values()), default=0),
                                max((car.lap for car in store.cars.values()), default=0)],
        "state_counts": snapshot["counts"],
    }


def read_metadata(path):
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))["cars"]
    if not isinstance(data, dict):
        raise ValueError("Metadata cars must be a dictionary keyed by exact car number")
    for identifier, values in data.items():
        if not isinstance(identifier, str) or not isinstance(values, dict):
            raise ValueError("Invalid metadata entry")
        for key in ("category", "team", "make", "source"):
            if values.get(key) is not None and not isinstance(values[key], str):
                raise ValueError("Metadata values must be strings or null")
    return data
