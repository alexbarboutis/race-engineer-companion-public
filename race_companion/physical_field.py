"""Validate HH bridge fields and expose measured traffic independently of pace."""

from copy import deepcopy
import math

from .state import number


def _identifier(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("Invalid physical-field car identifier")
    return value


def validate_field(message):
    if message.get("schema_version") != 1:
        raise ValueError("Unsupported physical-field schema")
    epoch = _identifier(message.get("session_epoch"))
    sequence = number(message["sequence"], integer=True, minimum=1)
    source_time = message.get("session_time_s")
    source_time = None if source_time is None else number(source_time)
    order = message.get("physical_order")
    raw_rows = message.get("cars")
    if not isinstance(order, list) or not isinstance(raw_rows, list):
        raise ValueError("Physical field requires order and car lists")
    order = [_identifier(value) for value in order]
    if len(set(order)) != len(order):
        raise ValueError("Duplicate physical order")
    reference = _identifier(message.get("reference_car"), optional=True)
    if reference is not None and reference not in order:
        raise ValueError("Display reference is outside physical order")
    if message.get("gap_method") != "last_measured_crossing":
        raise ValueError("Unsupported physical gap method")

    def gap(raw, rear, front):
        if raw is None:
            return None
        if not isinstance(raw, dict) or rear is None or front is None:
            raise ValueError("Invalid physical gap")
        if raw.get("from_car") != rear or raw.get("to_car") != front:
            raise ValueError("Physical gap direction disagrees with neighbours")
        seconds = number(raw["gap_s"])
        measured = number(raw["measured_at_session_time_s"])
        ahead_time = number(raw["ahead_crossing_session_time_s"])
        if (source_time is None or measured > source_time + 1e-6 or
                not math.isclose(measured - ahead_time, seconds, abs_tol=1e-4)):
            raise ValueError("Inconsistent physical gap timestamps")
        line = raw.get("timing_line")
        if line is not None and (not isinstance(line, str) or not line):
            raise ValueError("Invalid physical gap timing line")
        start_finish = raw.get("is_start_finish", False)
        if not isinstance(start_finish, bool):
            raise ValueError("Invalid physical gap line marker")
        return {"from_car": rear, "to_car": front, "gap_s": seconds,
                "measured_at_session_time_s": measured,
                "ahead_crossing_session_time_s": ahead_time,
                "timing_line": line, "is_start_finish": start_finish}

    rows = []
    seen = set()
    indexes = {car: index for index, car in enumerate(order)}
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValueError("Invalid physical-field row")
        identifier = _identifier(raw.get("car"))
        if identifier in seen:
            raise ValueError("Duplicate physical-field row")
        seen.add(identifier)
        active = raw.get("active")
        if not isinstance(active, bool) or active != (identifier in indexes):
            raise ValueError("Physical field active markers disagree with order")
        index = indexes.get(identifier)
        position = raw.get("physical_order")
        expected_position = index + 1 if active else None
        if position != expected_position or isinstance(position, bool):
            raise ValueError("Invalid physical order number")
        ahead = order[index - 1] if active and len(order) > 1 else None
        behind = order[(index + 1) % len(order)] if active and len(order) > 1 else None
        if raw.get("car_ahead") != ahead or raw.get("car_behind") != behind:
            raise ValueError("Physical neighbours disagree with circular order")
        crossing = raw.get("crossing_session_time_s")
        crossing = None if crossing is None else number(crossing)
        if active and (crossing is None or source_time is None or crossing > source_time + 1e-6):
            raise ValueError("Invalid physical-field crossing timestamp")
        state = raw.get("status")
        flag = raw.get("track_status")
        if any(value is not None and not isinstance(value, str) for value in (state, flag)):
            raise ValueError("Invalid physical-field status")
        if active and state != "OnTrackRunning":
            raise ValueError("Physical field marks a non-running car active")
        sector = raw.get("sector")
        if sector is not None and not isinstance(sector, str):
            raise ValueError("Invalid physical-field sector")
        rows.append({"car": identifier, "active": active, "status": state,
                     "track_status": flag, "sector": sector,
                     "lap": number(raw.get("lap", 0), integer=True),
                     "physical_order": position, "crossing_session_time_s": crossing,
                     "car_ahead": ahead, "car_behind": behind,
                     "gap_ahead": gap(raw.get("gap_ahead"), identifier, ahead),
                     "gap_behind": gap(raw.get("gap_behind"), behind, identifier)})
    if set(order) - seen:
        raise ValueError("Physical order contains missing rows")
    active_rows = {row["car"]: row for row in rows if row["active"]}
    for rear, row in active_rows.items():
        if row["car_ahead"] is not None:
            # A shared pair must be represented by the same measured sample.
            if row["gap_ahead"] != active_rows[row["car_ahead"]]["gap_behind"]:
                raise ValueError("Physical field has conflicting reciprocal gaps")
    return {"session_epoch": epoch, "sequence": sequence,
            "session_time_s": source_time, "reference_car": reference,
            "physical_order": order, "gap_method": "last_measured_crossing", "cars": rows}


def snapshot_field(field, now_received, source_time):
    if field is None:
        return None
    result = deepcopy(field)
    result["received_age_s"] = max(0, now_received - field["received_elapsed_s"])
    for row in result["cars"]:
        crossing = row["crossing_session_time_s"]
        row["source_crossing_age_s"] = (None if source_time is None or crossing is None
                                         else max(0, source_time - crossing))
        for key in ("gap_ahead", "gap_behind"):
            if row[key] is not None:
                row[key]["source_age_s"] = (None if source_time is None else
                    max(0, source_time - row[key]["measured_at_session_time_s"]))
    return result


def field_state(snapshot, maximum_age=150):
    field = snapshot.get("physical_field")
    if field is None:
        return {"source": "HH measured crossings", "ready": False, "cars": [],
                "reasons": ["Awaiting physical traffic data from the updated HH bridge; older recordings do not contain it"]}
    reasons = []
    if not snapshot["connected"]:
        reasons.append("Physical field feed is disconnected")
    if field["received_age_s"] > maximum_age:
        reasons.append("HH physical field update is too old")
    by_number = {row["car"]: row for row in field["cars"]}
    rows = []
    for identifier in field["physical_order"]:
        row = by_number[identifier]
        row_reasons = []
        crossing_age = row["source_crossing_age_s"]
        if crossing_age is None or crossing_age > maximum_age:
            row_reasons.append("sector crossing is too old")
        if len(field["physical_order"]) > 1:
            for direction in ("ahead", "behind"):
                sample = row["gap_" + direction]
                if sample is None:
                    row_reasons.append("missing measured gap " + direction)
                elif sample["source_age_s"] is None or sample["source_age_s"] > maximum_age:
                    row_reasons.append("measured gap " + direction + " is too old")
        reasons.extend(f"Car {identifier}: {reason}" for reason in row_reasons)
        ahead, behind = row["gap_ahead"], row["gap_behind"]
        rows.append({"number": identifier, "physical_order": row["physical_order"],
                     "physical_car_ahead": row["car_ahead"],
                     "physical_gap_ahead_s": ahead["gap_s"] if ahead else None,
                     "physical_car_behind": row["car_behind"],
                     "physical_gap_behind_s": behind["gap_s"] if behind else None,
                     "physical_gap_age_s": ahead["source_age_s"] if ahead else None,
                     "physical_gap_behind_age_s": behind["source_age_s"] if behind else None,
                     "gap_ahead": ahead, "gap_behind": behind,
                     "ready": not row_reasons and snapshot["connected"] and field["received_age_s"] <= maximum_age})
    if not rows:
        reasons.append("HH physical field has no observed on-track cars")
    return {"source": "HH measured crossings", "ready": not reasons, "reasons": reasons,
            "reference_car": field["reference_car"], "received_age_s": field["received_age_s"],
            "gap_method": field["gap_method"], "cars": rows}
