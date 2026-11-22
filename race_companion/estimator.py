"""Prepare a simple constant-pace state from observed timing messages."""

from statistics import median

from .physical_field import field_state as measured_field_state
from .state import PIT_STATUSES, number


EXCLUDED_STATUSES = PIT_STATUSES | {"Retired", "Excluded"}


def prepare_model(snapshot, profile):
    """Readiness and linear progress estimation; no car-to-car interactions."""
    now = snapshot.get("latest_session_time_s")
    limit = number(profile.get("maximum_source_age_s", 150), minimum=.001)
    minimum = number(profile.get("minimum_clean_laps", 2), integer=True, minimum=1)
    fractions = profile.get("timing_line_lap_fractions", [.32, .65, 1])
    if (not isinstance(fractions, list) or len(fractions) != 3 or
            not 0 < number(fractions[0]) < number(fractions[1]) < number(fractions[2]) == 1):
        raise ValueError("Timing line fractions must satisfy 0 < S1 < S2 < S3 = 1")
    fractions = [number(value) for value in fractions]
    field = measured_field_state(snapshot, limit)
    missing = []
    if not snapshot["connected"]:
        missing.append("Feed is disconnected")
    if now is None:
        missing.append("No source session timestamps")
    clock_age = snapshot.get("source_clock_age_s")
    if clock_age is not None and clock_age > number(profile.get("maximum_clock_silence_s", 60), minimum=.001):
        missing.append("Source session clock has stopped advancing")
    expected = profile.get("expected_car_numbers", [])
    if (not isinstance(expected, list) or any(not isinstance(x, str) or not x for x in expected)
            or len(expected) != len(set(expected))):
        raise ValueError("Expected car numbers must be unique nonempty strings")
    excluded = [{"number": car["number"], "status": car["status"]}
                for car in snapshot["cars"] if car.get("status") in EXCLUDED_STATUSES]
    active = [car for car in snapshot["cars"] if car.get("status") not in EXCLUDED_STATUSES]
    received = {car["number"] for car in active}
    absent = sorted(set(expected) - received - {car["number"] for car in excluded})
    unreported = []
    if snapshot["as_of_received_elapsed_s"] >= number(profile.get("unreported_car_grace_s", 150), minimum=.001):
        unreported = [{"number": value, "status": "Not received"} for value in absent]
        absent = []
    if absent:
        missing.append("Awaiting expected cars: " + ", ".join(absent))
    if not active:
        missing.append("No active cars received")
    readiness = {"ready_cars": 0, "received_cars": len(active), "field_size": len(active) + len(absent),
                 "minimum_clean_laps": minimum, "excluded_cars": excluded, "unreported_cars": unreported,
                 "absent_cars": absent, "cars": {}}
    prepared = []
    for car in active:
        reasons = []
        crossing = car.get("last_crossing")
        age = None if crossing is None or now is None else now - crossing["session_time_s"]
        if age is None:
            reasons.append("missing timestamped crossing")
        elif age < 0 or age > limit:
            reasons.append("source crossing too old")
        if car.get("status") != "OnTrackRunning":
            reasons.append("car is not confirmed running")
        if car.get("track_status") != "Green":
            reasons.append("track is not confirmed Green")
        pit_time = car.get("last_pit_status_session_time_s")
        if pit_time is not None and (crossing is None or crossing["session_time_s"] <= pit_time):
            reasons.append("waiting for a crossing after the pit visit")
        clean = sorted((int(lap), row["last_lap"]) for lap, row in car["lap_history"].items()
                       if int(lap) > 1 and row.get("in_lap") is False and row.get("out_lap") is False
                       and row.get("track_status") == "Green" and row.get("last_lap", 0) > 0)
        if len(clean) < minimum:
            reasons.append(f"have {len(clean)}/{minimum} green laps after lap 1; need pace")
        readiness["cars"][car["number"]] = {
            "ready": not reasons, "reasons": reasons, "clean_laps": len(clean),
            "pace_source": "median_recent_green_laps", "source_crossing_age_s": age,
            "maximum_source_age_s": limit, "last_crossing": crossing}
        readiness["ready_cars"] += int(not reasons)
        if reasons:
            missing.append(f"Car {car['number']}: " + "; ".join(reasons))
            continue
        pace = median(value for _, value in clean[-5:])
        sector = int(crossing["sector"][1]) - 1
        # Linear distance between observations is deliberately a simple baseline.
        distance = crossing["lap"] - 1 + fractions[sector] + age / pace
        prepared.append({"number": car["number"], "category": car.get("category"),
                         "lap_time_s": pace, "distance_laps": distance})
    field_numbers = {car["number"] for car in field["cars"]}
    field_matches = field_numbers == received
    if not field_matches:
        missing.append("Physical field is awaiting the current active cars")
    missing.extend(field["reasons"])
    readiness["physical_state"] = {"ready": field["ready"] and field_matches,
        "ready_cars": sum(car["ready"] for car in field["cars"]), "field_size": len(field["cars"])}
    result = {"status": "waiting_for_inputs" if missing else "ready_for_baseline",
        "as_of_session_time_s": now, "missing": missing, "readiness": readiness,
        "field_state": field, "active_car_numbers": sorted(received),
        "assumptions": [
            "Constant pace; cars do not affect each other's lap times.",
            "Future times are one and two selected-car pace laps after the source snapshot.",
            "Pit loss is a simple time delay, not a simulated pit route.",
            "Forecast physical gaps are lap-fraction separations expressed at the selected car's pace.",
            "Current measured gaps are asynchronous crossing observations, separate from estimated future gaps.",
            "Positions cover the reported on-track field; rival stops and rejoining cars are not predicted."]}
    if not missing:
        result["model_input"] = {"cars": prepared, "source_time_s": now, "assumptions": result["assumptions"]}
    return result
