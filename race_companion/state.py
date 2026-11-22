"""Normalize the supplied HH message schema without inferring track position.

The raw recorder keeps fields this adapter does not yet understand. Source
timestamp and session semantics must be verified against an actual capture.
"""

from collections import Counter
from dataclasses import asdict, dataclass, field
import json
import math


MESSAGE_TYPES = {
    "car", "driver", "status", "sector", "lap", "stint", "pit", "speed", "position",
    "gap", "feed_result", "track_gap", "track_distance", "physical_field",
}
MAX_PLAUSIBLE_SPEED_KMH = 500
PIT_STATUSES = {"PitIn", "PitOut", "InPitStop", "InPitOut"}


def number(value, *, integer=False, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Expected a number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < minimum:
        raise ValueError("Number outside allowed range")
    if integer and not parsed.is_integer():
        raise ValueError("Expected an integer")
    return int(parsed) if integer else parsed


@dataclass
class CarState:
    number: str
    category: str | None = None
    team: str | None = None
    make: str | None = None
    driver: str | None = None
    status: str | None = None
    track_status: str | None = None
    last_lap_session_time_s: float | None = None
    status_session_time_s: float | None = None
    last_pit_status_session_time_s: float | None = None
    projected_lap_time: float | None = None
    last_crossing: dict | None = None
    lap: int = 0
    last_lap: float | None = None
    position: int | None = None
    class_position: int | None = None
    updated_position: int | None = None
    updated_class_position: int | None = None
    position_update_received_elapsed_s: float | None = None
    class_position_update_received_elapsed_s: float | None = None
    hh_gap_to_leader_s: float | None = None
    hh_interval_s: float | None = None
    hh_laps_back: int | None = None
    hh_gap_update_type: str | None = None
    hh_gap_received_elapsed_s: float | None = None
    feed_gap_to_leader_s: float | None = None
    feed_gap_to_class_leader_s: float | None = None
    track_distance: float | None = None
    track_distance_received_elapsed_s: float | None = None
    stint: int | None = None
    stint_lap: int | None = None
    pit_stop: int | None = None
    sector_lap: int | None = None
    s1: float | None = None
    s2: float | None = None
    s3: float | None = None
    max_speed_kmh: float | None = None
    max_speed_trap: str | None = None
    max_speed_lap: int | None = None
    max_speed_crossing_time_s: float | None = None
    speed_trap_maxima: dict = field(default_factory=dict)
    last_seen_elapsed_s: float | None = None
    last_timing_elapsed_s: float | None = None
    field_received_elapsed_s: dict = field(default_factory=dict)
    field_source_session_s: dict = field(default_factory=dict)
    lap_history: dict = field(default_factory=dict)
    sector_history: dict = field(default_factory=dict)


class TimingStore:
    """Single writer; snapshots are detached copies suitable for a GUI queue.

    A lower lap is ignored within a connection epoch. A reset requires a new
    recording or explicit reset(). Reconnect starts an empty epoch because
    the supplied schema has no verified session identifier or replay marker.
    """

    def __init__(self, history_limit=30, metadata=None):
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self.history_limit = history_limit
        self.metadata = metadata or {}
        self.cars = {}
        self.counts = Counter()
        self.connected = False
        self.connection_epoch = 0
        self.connection_detail = "Waiting for data"
        self.latest_session_time_s = None
        self.latest_session_received_elapsed_s = None
        self.track_gaps = {}
        self.physical_field = None

    def reset(self):
        self.cars.clear()
        self.latest_session_time_s = None
        self.latest_session_received_elapsed_s = None
        self.track_gaps.clear()
        self.physical_field = None

    def consume(self, record):
        kind = record.get("kind")
        if kind == "connection":
            state = record.get("state")
            self.connection_detail = record.get("detail", state)
            self.connected = state == "connected"
            if self.connected:
                self.connection_epoch += 1
                self.reset()
            return
        if kind != "message":
            return
        self.counts["received_lines"] += 1
        try:
            message = json.loads(record["raw"], parse_constant=self._bad_constant)
            if not isinstance(message, dict):
                raise ValueError("Expected a JSON object")
            elapsed_s = number(record["received_elapsed_s"])
            msg_type = message.get("type")
            if not isinstance(msg_type, str):
                raise ValueError("Expected a message type")
            if msg_type not in MESSAGE_TYPES:
                self.counts["unknown_type"] += 1
                return
            self._apply(message, elapsed_s)
        except (ValueError, TypeError, KeyError, OverflowError):
            self.counts["invalid_messages"] += 1

    @staticmethod
    def _bad_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    def _apply(self, message, elapsed_s):
        if message["type"] == "physical_field":
            from .physical_field import validate_field
            field = validate_field(message)
            previous = self.physical_field
            if previous is not None and previous["session_epoch"] == field["session_epoch"]:
                if field["sequence"] <= previous["sequence"]:
                    self.counts["older_physical_field_ignored"] += 1
                    return
            elif previous is not None:
                self.reset()
                self.connection_epoch += 1
            field["received_elapsed_s"] = elapsed_s
            self.physical_field = field
            source_time = field["session_time_s"]
            if source_time is not None and (self.latest_session_time_s is None or source_time > self.latest_session_time_s):
                self.latest_session_time_s = source_time
                self.latest_session_received_elapsed_s = elapsed_s
            # A newly connected client can first learn of cars from a field
            # snapshot. Seed only reported observations; do not invent pace.
            for row in field["cars"]:
                car = self.cars.setdefault(row["car"], CarState(number=row["car"]))
                if car.status is None:
                    car.status = row["status"]
                if car.track_status is None:
                    car.track_status = row["track_status"]
                if car.last_crossing is None and row["sector"] in ("S1", "S2", "S3") and row["crossing_session_time_s"] is not None:
                    car.last_crossing = {"lap": row["lap"], "sector": row["sector"],
                        "session_time_s": row["crossing_session_time_s"],
                        "received_elapsed_s": elapsed_s, "timestamp_field": "physical_field"}
            self.counts["accepted_messages"] += 1
            return
        car_number = message["car"]
        if isinstance(car_number, bool) or not isinstance(car_number, (str, int)):
            raise ValueError("Expected a car number")
        car_number = str(car_number).strip()
        if not car_number:
            raise ValueError("Empty car number")

        kind = message["type"]
        delta = {}
        text_fields = {
            "car": ("category", "team", "make"), "driver": ("driver",),
            "status": ("status",), "lap": ("driver", "track_status"),
            "sector": ("driver", "track_status"), "stint": ("driver",),
        }.get(kind, ())
        for key in text_fields:
            value = message.get(key)
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                raise ValueError(f"Invalid {key}")
            if key == "category" and value == "%^&*&^%":
                continue
            delta[key] = value

        int_fields = {
            "lap": ("lap", "position", "class_position", "stint", "stint_lap"),
            "stint": ("stint",), "pit": ("pit_stop",),
        }.get(kind, ())
        for key in int_fields:
            if message.get(key) is not None:
                delta[key] = number(message[key], integer=True)
        if kind == "lap":
            if "lap" not in delta:
                raise ValueError("Lap update requires lap number")
            if message.get("lap_time") is not None:
                lap_time = number(message["lap_time"], minimum=0.001)
                delta["last_lap"] = lap_time

        if kind == "sector":
            sector_lap = number(message["lap"], integer=True)
            sector = message["sector"]
            if sector not in ("S1", "S2", "S3"):
                raise ValueError("Unrecognized sector")
            sector_time = number(message["sector_time"], minimum=0.001)

        if kind == "speed":
            if message.get("unit") != "km/h":
                raise ValueError("Speed unit must be km/h")
            speed_kmh = number(message["speed_kmh"], minimum=0.001)
            if speed_kmh > MAX_PLAUSIBLE_SPEED_KMH:
                raise ValueError("Speed exceeds plausible circuit-racing range")
            trap = message["trap"]
            if not isinstance(trap, str) or not trap.strip():
                raise ValueError("Speed trap name is required")
            trap = trap.strip()
            speed_lap = number(message["lap"], integer=True)
            crossing_time = message.get("trap_crossing_time")
            if crossing_time is not None:
                crossing_time = number(crossing_time)

        if kind == "position":
            rank = number(message["position"], integer=True, minimum=1)
            rank_type = message["position_type"]
            location = message["update_location"]
            if rank_type not in ("Overall", "Category") or location not in ("Lap", "Sector"):
                raise ValueError("Unrecognized HH position update")

        if kind == "gap":
            hh_gap = number(message["gap"])
            hh_interval = (None if message.get("interval") is None else
                           number(message["interval"]))
            hh_laps_back = number(message.get("laps_back", 0), integer=True)
            hh_gap_type = message["gap_type"]
            hh_gap_update_type = message["update_type"]
            if hh_gap_type not in ("Overall", "Category"):
                raise ValueError("Unrecognized HH gap type")
            if hh_gap_update_type not in ("EndOfLap", "Sector"):
                raise ValueError("Unrecognized HH gap update type")

        if kind == "feed_result":
            feed_position = (None if message.get("position") is None else
                             number(message["position"], integer=True, minimum=1))
            feed_class_position = (None if message.get("class_position") is None else
                                   number(message["class_position"], integer=True, minimum=1))
            feed_gap = (None if message.get("gap_to_leader") is None else
                        number(message["gap_to_leader"]))
            feed_class_gap = (None if message.get("gap_to_class_leader") is None else
                              number(message["gap_to_class_leader"]))

        if kind == "track_distance":
            track_distance = number(message["track_distance"], minimum=-float("inf"))
            if not isinstance(message.get("is_safety_car", False), bool):
                raise ValueError("Invalid safety-car marker")

        if kind == "track_gap":
            raw_gaps = message.get("gaps_behind")
            if not isinstance(raw_gaps, list):
                raise ValueError("HH track gap requires a list")
            track_gap_rows = []
            seen_gap_cars = set()
            for item in raw_gaps:
                if not isinstance(item, dict):
                    raise ValueError("Invalid HH track-gap item")
                gap_car = item.get("car")
                if isinstance(gap_car, bool) or not isinstance(gap_car, (str, int)):
                    raise ValueError("Invalid HH track-gap car")
                gap_car = str(gap_car).strip()
                if not gap_car or gap_car == car_number or gap_car in seen_gap_cars:
                    raise ValueError("Duplicate or invalid HH track-gap car")
                seen_gap_cars.add(gap_car)
                is_safety_car = item.get("is_safety_car", False)
                if not isinstance(is_safety_car, bool):
                    raise ValueError("Invalid track-gap safety-car marker")
                track_gap_rows.append({
                    "number": gap_car,
                    "gap_s": number(item["gap"]),
                    "session_time_s": number(item["session_time"]),
                    "is_safety_car": is_safety_car,
                })
            if not isinstance(message.get("is_safety_car", False), bool):
                raise ValueError("Invalid reference safety-car marker")
            if not isinstance(message.get("is_start_finish", False), bool):
                raise ValueError("Invalid start-finish marker")

        source_key = {"lap": "elapsed_time", "sector": "sector_exit_time",
                      "status": "session_time"}.get(kind)
        source_time = None
        if source_key and message.get(source_key) is not None:
            source_time = number(message[source_key])
        if kind == "lap" and source_time is not None:
            delta["last_lap_session_time_s"] = source_time
        if kind == "status" and source_time is not None:
            delta["status_session_time_s"] = source_time
        if kind == "sector" and message.get("projected_lap_time") is not None:
            delta["projected_lap_time"] = number(message["projected_lap_time"], minimum=0.001)
        flags = {}
        if kind == "lap":
            for key in ("in_lap", "out_lap"):
                if message.get(key) is not None:
                    if not isinstance(message[key], bool):
                        raise ValueError(f"Invalid {key}")
                    flags[key] = message[key]

        # Validate before creating or changing any car state.
        car = self.cars.setdefault(car_number, CarState(number=car_number))
        car.last_seen_elapsed_s = elapsed_s
        if kind == "speed":
            observation = {"speed_kmh": speed_kmh, "lap": speed_lap,
                           "crossing_time_s": crossing_time, "received_elapsed_s": elapsed_s}
            previous = car.speed_trap_maxima.get(trap)
            if previous is None or speed_kmh > previous["speed_kmh"]:
                car.speed_trap_maxima[trap] = observation
            if car.max_speed_kmh is None or speed_kmh > car.max_speed_kmh:
                car.max_speed_kmh = speed_kmh
                car.max_speed_trap = trap
                car.max_speed_lap = speed_lap
                car.max_speed_crossing_time_s = crossing_time
            self.counts["accepted_messages"] += 1
            return
        if kind == "position":
            if rank_type == "Overall":
                car.updated_position = rank
                car.position_update_received_elapsed_s = elapsed_s
            else:
                car.updated_class_position = rank
                car.class_position_update_received_elapsed_s = elapsed_s
            self.counts["accepted_messages"] += 1
            return
        if kind == "gap":
            if hh_gap_type == "Overall":
                car.hh_gap_to_leader_s = hh_gap
                car.hh_interval_s = hh_interval
                car.hh_laps_back = hh_laps_back
                car.hh_gap_update_type = hh_gap_update_type
                car.hh_gap_received_elapsed_s = elapsed_s
            self.counts["accepted_messages"] += 1
            return
        if kind == "feed_result":
            if feed_position is not None:
                car.updated_position = feed_position
                car.position_update_received_elapsed_s = elapsed_s
            if feed_class_position is not None:
                car.updated_class_position = feed_class_position
                car.class_position_update_received_elapsed_s = elapsed_s
            if feed_gap is not None:
                car.feed_gap_to_leader_s = feed_gap
            if feed_class_gap is not None:
                car.feed_gap_to_class_leader_s = feed_class_gap
            self.counts["accepted_messages"] += 1
            return
        if kind == "track_distance":
            car.track_distance = track_distance
            car.track_distance_received_elapsed_s = elapsed_s
            self.counts["accepted_messages"] += 1
            return
        if kind == "track_gap":
            self.track_gaps[car_number] = {
                "reference_car": car_number,
                "gaps_behind": track_gap_rows,
                "is_safety_car": message.get("is_safety_car", False),
                "is_start_finish": message.get("is_start_finish", False),
                "received_elapsed_s": elapsed_s,
            }
            source_times = [item["session_time_s"] for item in track_gap_rows]
            if source_times:
                newest = max(source_times)
                if self.latest_session_time_s is None or newest > self.latest_session_time_s:
                    self.latest_session_time_s = newest
                    self.latest_session_received_elapsed_s = elapsed_s
            self.counts["accepted_messages"] += 1
            return
        if kind == "status" and source_time is not None and car.status_session_time_s is not None:
            if source_time < car.status_session_time_s:
                self.counts["older_status_ignored"] += 1
                return
        if kind == "status" and delta.get("status") in PIT_STATUSES and source_time is not None:
            car.last_pit_status_session_time_s = source_time
        if kind == "lap":
            lap = delta["lap"]
            if lap < car.lap:
                self.counts["older_lap_ignored"] += 1
                return
            previous = car.lap_history.get(lap, {}).get("last_lap_session_time_s")
            if previous is not None and source_time is not None and source_time < previous:
                self.counts["older_source_lap_ignored"] += 1
                return
            if lap > car.lap and "last_lap" not in delta:
                # Never label a previous lap's time with the new lap number.
                car.last_lap = None
                car.field_received_elapsed_s.pop("last_lap", None)
            history = car.lap_history.setdefault(lap, {"lap": lap})
            history.update(delta)
            history.update(flags)
            history["received_elapsed_s"] = elapsed_s
            self._trim(car.lap_history)
        elif kind == "sector":
            if car.sector_lap is not None and sector_lap < car.sector_lap:
                self.counts["older_sector_ignored"] += 1
                return
            if sector_lap < car.lap:
                self.counts["older_sector_ignored"] += 1
                return
            previous = car.sector_history.get(sector_lap, {}).get(f"{sector.lower()}_session_time_s")
            if previous is not None and source_time is not None and source_time < previous:
                self.counts["older_source_sector_ignored"] += 1
                return
            if sector_lap != car.sector_lap:
                car.sector_lap = sector_lap
                car.s1 = car.s2 = car.s3 = None
                for key in ("s1", "s2", "s3"):
                    car.field_received_elapsed_s.pop(key, None)
            delta[sector.lower()] = sector_time
            history = car.sector_history.setdefault(sector_lap, {"lap": sector_lap})
            history[sector.lower()] = sector_time
            history[f"{sector.lower()}_received_elapsed_s"] = elapsed_s
            if source_time is not None:
                history[f"{sector.lower()}_session_time_s"] = source_time
            history["track_status"] = delta.get("track_status")
            history.setdefault("sector_track_status", {})[sector.lower()] = delta.get("track_status")
            self._trim(car.sector_history)

        for key, value in delta.items():
            previous_source = car.field_source_session_s.get(key)
            if source_time is not None and previous_source is not None and source_time < previous_source:
                continue
            setattr(car, key, value)
            car.field_received_elapsed_s[key] = elapsed_s
            if source_time is not None:
                car.field_source_session_s[key] = source_time
        if source_time is not None:
            if self.latest_session_time_s is None or source_time > self.latest_session_time_s:
                self.latest_session_time_s = source_time
                self.latest_session_received_elapsed_s = elapsed_s
        if kind in ("lap", "sector"):
            if source_time is None:
                car.last_timing_elapsed_s = elapsed_s
            elif car.last_crossing is None or source_time >= car.last_crossing["session_time_s"]:
                # Never move the crossing anchor backwards after a late event.
                same_crossing = (car.last_crossing is not None and
                                 abs(source_time - car.last_crossing["session_time_s"]) < 1e-6)
                received = (car.last_crossing["received_elapsed_s"] if same_crossing else elapsed_s)
                car.last_crossing = {
                    "lap": delta["lap"] if kind == "lap" else sector_lap,
                    "sector": "S3" if kind == "lap" else sector,
                    "session_time_s": source_time,
                    "received_elapsed_s": received,
                    "timestamp_field": source_key,
                }
                car.last_timing_elapsed_s = received
        self.counts["accepted_messages"] += 1

    def _trim(self, history):
        while len(history) > self.history_limit:
            del history[min(history)]

    def snapshot(self, now_elapsed_s, stale_after_s=30.0):
        from .physical_field import snapshot_field
        if stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        rows = []
        for car in self.cars.values():
            row = asdict(car)
            row["metadata_source"] = {}
            for key in ("category", "team", "make"):
                if row[key]:
                    row["metadata_source"][key] = "HH"
                elif self.metadata.get(car.number, {}).get(key):
                    row[key] = self.metadata[car.number][key]
                    row["metadata_source"][key] = self.metadata[car.number].get("source", "manual_metadata")
                else:
                    row["metadata_source"][key] = "missing"
            age = (None if car.last_timing_elapsed_s is None else
                   max(0, now_elapsed_s - car.last_timing_elapsed_s))
            row["timing_age_s"] = age
            row["timing_stale"] = not self.connected or age is None or age > stale_after_s
            position_received = car.field_received_elapsed_s.get("position")
            if (car.updated_position is not None and
                    (position_received is None or
                     car.position_update_received_elapsed_s >= position_received)):
                row["lap_position"] = car.position
                row["position"] = car.updated_position
                row["position_source"] = "HH update"
                position_received = car.position_update_received_elapsed_s
            else:
                row["lap_position"] = car.position
                row["position_source"] = "Last lap" if car.position is not None else None
            class_position_received = car.field_received_elapsed_s.get("class_position")
            if (car.updated_class_position is not None and
                    (class_position_received is None or
                     car.class_position_update_received_elapsed_s >= class_position_received)):
                row["class_position"] = car.updated_class_position
            row["position_age_s"] = (None if position_received is None else
                                     max(0, now_elapsed_s - position_received))
            row["hh_gap_age_s"] = (None if car.hh_gap_received_elapsed_s is None else
                                   max(0, now_elapsed_s - car.hh_gap_received_elapsed_s))
            if car.hh_gap_to_leader_s is not None:
                row["race_gap_to_leader_s"] = car.hh_gap_to_leader_s
                row["race_laps_back"] = car.hh_laps_back or 0
                row["race_gap_source"] = "HH calculated"
            elif car.feed_gap_to_leader_s is not None:
                row["race_gap_to_leader_s"] = car.feed_gap_to_leader_s
                row["race_laps_back"] = 0
                row["race_gap_source"] = "Timing feed"
            elif row["position"] == 1:
                row["race_gap_to_leader_s"] = 0.0
                row["race_laps_back"] = 0
                row["race_gap_source"] = "Leader"
            else:
                row["race_gap_to_leader_s"] = None
                row["race_laps_back"] = None
                row["race_gap_source"] = None
            rows.append(row)
        rows.sort(key=lambda row: (row["position"] is None,
                                  row["position"] or 0, row["number"]))
        track_gaps = {}
        for reference, state in self.track_gaps.items():
            track_gaps[reference] = {
                **state,
                "received_age_s": max(0, now_elapsed_s - state["received_elapsed_s"]),
                "gaps_behind": [{
                    **item,
                    "source_age_s": (None if self.latest_session_time_s is None else
                                     max(0, self.latest_session_time_s - item["session_time_s"])),
                } for item in state["gaps_behind"]],
            }
        return {
            "as_of_received_elapsed_s": now_elapsed_s,
            "latest_session_time_s": self.latest_session_time_s,
            "source_clock_age_s": (None if self.latest_session_received_elapsed_s is None else
                                   max(0, now_elapsed_s - self.latest_session_received_elapsed_s)),
            "connected": self.connected,
            "connection_epoch": self.connection_epoch,
            "connection_detail": self.connection_detail,
            "counts": dict(self.counts),
            "cars": rows,
            "track_gaps": track_gaps,
            "physical_field": snapshot_field(self.physical_field, now_elapsed_s, self.latest_session_time_s),
            "prediction": {
                "status": "unavailable",
                "reason": "Timing only. Choose a car and event to enable the baseline projection.",
            },
        }
