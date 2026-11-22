"""Deterministic fictional timing events; no recorded race or model outputs."""

from datetime import datetime, timedelta, timezone
import json
import time


SPEEDUP = 20
FRACTIONS = (.32, .65, 1.0)
# Exact ids, lap pace, initial offset, category, fictional driver.
CARS = (("101", 95, 0, "Prototype", "Alex Example"),
        ("202", 96.5, 5, "Prototype", "Morgan Example"),
        ("303", 98, 10, "Prototype", "Sam Example"),
        ("404", 105, 15, "GT", "Jordan Example"),
        ("505", 106, 20, "GT", "Taylor Example"),
        ("606", 107, 25, "GT", "Casey Example"))


def synthetic_records(laps=8):
    if type(laps) is not int or not 1 <= laps <= 100:
        raise ValueError("Demo laps must be an integer from 1 to 100")
    records = []
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def envelope(source, kind, **fields):
        elapsed = source / SPEEDUP
        records.append({"schema_version": 1, "recording_id": "fictional-public-demo-v1",
            "sequence": len(records) + 1, "received_elapsed_s": elapsed,
            "received_utc": (origin + timedelta(seconds=elapsed)).isoformat(), "kind": kind, **fields})

    def emit(source, message):
        envelope(source, "message", raw=json.dumps(message, allow_nan=False))

    envelope(0, "connection", state="connected", detail="Synthetic demonstration (fictional data)")
    for index, (identifier, pace, offset, category, driver) in enumerate(CARS):
        emit(0, {"type": "car", "car": identifier, "category": category, "team": f"Demo team {index + 1}"})
        emit(0, {"type": "driver", "car": identifier, "driver": driver})
        emit(0, {"type": "status", "car": identifier, "status": "OnTrackRunning", "session_time": 0})
        emit(0, {"type": "speed", "car": identifier, "lap": 0, "trap": "Demo trap",
                 "unit": "km/h", "speed_kmh": 280 - index * 5})
    schedule = sorted((offset + (lap - 1 + fraction) * pace, identifier, lap, sector)
                      for identifier, pace, offset, _, _ in CARS
                      for lap in range(1, laps + 1) for sector, fraction in enumerate(FRACTIONS, 1))
    configurations = {car[0]: car for car in CARS}
    last = {}
    histories = {car[0]: {1: [], 2: [], 3: []} for car in CARS}
    field_sequence = 0
    for source, identifier, lap, sector in schedule:
        _, pace, _, category, driver = configurations[identifier]
        fraction_length = FRACTIONS[sector - 1] - (FRACTIONS[sector - 2] if sector > 1 else 0)
        emit(source, {"type": "sector", "car": identifier, "lap": lap, "sector": f"S{sector}",
            "sector_time": pace * fraction_length, "sector_exit_time": source, "track_status": "Green"})
        last[identifier] = (source, lap, sector)
        histories[identifier][sector].append(source)
        progress = {car[0]: max(0, (source - car[2]) / car[1]) for car in CARS}
        ranking = sorted(progress, key=lambda car: (-progress[car], car))
        if sector == 3:
            class_ranking = [car for car in ranking if configurations[car][3] == category]
            emit(source, {"type": "lap", "car": identifier, "lap": lap, "lap_time": pace,
                "elapsed_time": source, "in_lap": False, "out_lap": False, "track_status": "Green",
                "position": ranking.index(identifier) + 1, "class_position": class_ranking.index(identifier) + 1,
                "driver": driver})
        order = sorted(last, key=lambda car: (-(progress[car] % 1), car))
        reference = next((car for car in ranking if car in order), None)
        if reference:
            anchor = order.index(reference)
            order = order[anchor:] + order[:anchor]
        gaps = {}
        for index, rear in enumerate(order):
            if len(order) == 1:
                gaps[rear] = None
                continue
            front = order[index - 1]
            crossing, _, line = last[rear]
            earlier = [value for value in histories[front][line] if value <= crossing]
            gaps[rear] = None if not earlier else {
                "from_car": rear, "to_car": front, "gap_s": crossing - earlier[-1],
                "measured_at_session_time_s": crossing, "ahead_crossing_session_time_s": earlier[-1],
                "timing_line": f"S{line}", "is_start_finish": line == 3}
        rows = []
        for car in configurations:
            active = car in order
            index = order.index(car) if active else None
            crossing = last.get(car)
            ahead = order[index - 1] if active and len(order) > 1 else None
            behind = order[(index + 1) % len(order)] if active and len(order) > 1 else None
            rows.append({"car": car, "active": active, "status": "OnTrackRunning", "track_status": "Green",
                "lap": crossing[1] if crossing else 0, "sector": f"S{crossing[2]}" if crossing else None,
                "crossing_session_time_s": crossing[0] if crossing else None,
                "physical_order": index + 1 if active else None, "car_ahead": ahead, "car_behind": behind,
                "gap_ahead": gaps[car] if active else None, "gap_behind": gaps[behind] if behind else None})
        field_sequence += 1
        emit(source, {"type": "physical_field", "schema_version": 1,
            "session_epoch": "fictional-demo-session-v1", "sequence": field_sequence,
            "gap_method": "last_measured_crossing", "source": "Synthetic timing generator",
            "session_time_s": source, "reference_car": reference, "physical_order": order, "cars": rows})
    return records


def demo_records(*, paced=False, stop_event=None):
    previous = 0
    for record in synthetic_records():
        if stop_event is not None and stop_event.is_set():
            return
        delay = record["received_elapsed_s"] - previous
        if paced and delay > 0:
            if stop_event is not None:
                if stop_event.wait(delay):
                    return
            else:
                time.sleep(delay)
        previous = record["received_elapsed_s"]
        yield record
