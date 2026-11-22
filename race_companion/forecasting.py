"""Shared forecast path and a single coalescing worker for live capture.

Only detached snapshots enter this module. The worker never touches the
TimingStore or socket. At most one projection runs; new snapshots replace
the candidate for the next run instead of building an unbounded queue.
"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from .diagnostics import read_metadata
from .estimator import PIT_STATUSES, prepare_model
from .pits import load_scenarios
from .state import number


def read_event(path, metadata_path=None):
    profile = json.loads(path.read_text(encoding="utf-8")) if path else None
    if profile is not None and not isinstance(profile, dict):
        raise ValueError("Event profile must be a JSON object")
    if metadata_path is None and profile and profile.get("metadata_file"):
        metadata_path = path.parent / profile["metadata_file"]
    return profile, read_metadata(metadata_path)


def event_file(event_path, profile, explicit_path, key, default=None):
    if explicit_path is not None:
        return explicit_path
    if profile and profile.get(key):
        return event_path.parent / profile[key]
    return Path(default) if default else None


class ForecastRunner:
    def __init__(self, profile, parameters, selected_car, scenarios=(), projector=None):
        if projector is None:
            from .engine import project
            projector = project
        self.profile = profile
        self.parameters = parameters
        self.selected_car = selected_car
        self.scenarios = scenarios
        self.projector = projector

    @classmethod
    def from_args(cls, args, profile):
        parameters = profile.get("baseline", {})
        pit_path = event_file(args.event, profile, args.pits, "pit_scenarios_file")
        return cls(profile, parameters, args.car, load_scenarios(pit_path) if pit_path else ())

    def prepare(self, snapshot):
        prepared = prepare_model(snapshot, self.profile)
        prepared["selected_car"] = self.selected_car
        prepared["event"] = self.profile.get("name")
        prepared["calibration_note"] = self.profile.get("calibration_note")
        prepared["as_of_received_elapsed_s"] = snapshot["as_of_received_elapsed_s"]
        excluded = {car["number"]: car["status"]
                    for car in prepared["readiness"]["excluded_cars"]}
        if self.selected_car in excluded:
            prepared["status"] = "waiting_for_inputs"
            if excluded[self.selected_car] in PIT_STATUSES:
                reason = (f"Selected car {self.selected_car} is {excluded[self.selected_car]}; "
                          "wait for it to return to the track and cross a timing line")
            else:
                reason = (f"Selected car {self.selected_car} is {excluded[self.selected_car]}; "
                          "choose an active car")
            prepared.setdefault("missing", []).append(reason)
        elif self.selected_car not in {car["number"] for car in snapshot["cars"]}:
            prepared["status"] = "waiting_for_inputs"
            prepared.setdefault("missing", []).append(f"Selected car {self.selected_car} has not arrived")
        return prepared

    def project(self, prepared):
        common = {key: value for key, value in prepared.items() if key != "model_input"}
        try:
            result = self.projector(prepared["model_input"], self.parameters,
                                    self.selected_car, self.scenarios)
            return {**common, **result}
        except (ValueError, KeyError, TypeError, RuntimeError, ArithmeticError) as exc:
            return {**common, "status": "forecast_error", "reason": str(exc)}

    def forecast(self, snapshot):
        prepared = self.prepare(snapshot)
        if prepared["status"] != "ready_for_baseline":
            return {k: v for k, v in prepared.items() if k != "model_input"}
        return self.project(prepared)


class LiveForecaster:
    def __init__(self, runner):
        self.runner = runner
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="forecast")
        self.future = None
        self.epoch = None
        self.generation = 0
        self.result = None
        self.last_submitted = None
        self.maximum_lag_s = number(runner.profile.get("maximum_forecast_lag_s", 10), minimum=0.001)
        self.hold_limit_s = number(runner.profile.get("stale_rival_hold_s", 0))
        self.hold_started_s = None
        self.held_car = None

    def _clear_result(self):
        self.generation += 1
        self.result = None
        self.last_submitted = None
        self.hold_started_s = None
        self.held_car = None

    def _held_result(self, snapshot, prepared):
        """Display a prior valid result, never project from a stale field.

        This narrowly tolerates one late rival crossing. Both receiver time
        and source-time lag are bounded, so a frozen source cannot extend
        the hold indefinitely. All other readiness failures still block.
        """
        if self.hold_limit_s <= 0 or self.result is None:
            return None
        if self.result.get("status") != "baseline_projection":
            return None
        rows = prepared["readiness"]["cars"]
        blocked = [(identifier, row) for identifier, row in rows.items() if not row["ready"]]
        if len(blocked) != 1:
            return None
        identifier, row = blocked[0]
        if identifier == self.runner.selected_car or row["reasons"] != ["source crossing too old"]:
            return None
        # Also rejects global failures, absent cars, flags, and missing selected car.
        if prepared.get("missing") != [f"Car {identifier}: source crossing too old"]:
            return None
        source_lag = prepared["as_of_session_time_s"] - self.result["as_of_session_time_s"]
        if not 0 <= source_lag <= min(self.hold_limit_s, self.maximum_lag_s):
            return None
        if row["source_crossing_age_s"] > row["maximum_source_age_s"] + self.hold_limit_s:
            return None
        received = snapshot["as_of_received_elapsed_s"]
        if self.hold_started_s is None:
            self.hold_started_s = received
            self.held_car = identifier
        if identifier != self.held_car or not 0 <= received - self.hold_started_s < self.hold_limit_s:
            return None
        return {
            **self.result, "status": "held_projection", "updating": False,
            "source_lag_s": source_lag,
            "current_source_session_time_s": prepared["as_of_session_time_s"],
            "current_readiness": prepared["readiness"],
            "hold": {
                "car": identifier, "source_crossing_age_s": row["source_crossing_age_s"],
                "last_crossing": row["last_crossing"],
                "receiver_elapsed_s": received - self.hold_started_s,
                "maximum_hold_s": self.hold_limit_s,
                "reason": "Showing the last valid forecast while one rival crossing is overdue. No new projection is calculated from stale inputs.",
            },
        }

    def update(self, snapshot):
        epoch = snapshot["connection_epoch"]
        if epoch != self.epoch:
            self.epoch = epoch
            self._clear_result()
        prepared = self.runner.prepare(snapshot)
        # A completed job still describes a field that was valid when submitted.
        if self.future is not None and self.future.done():
            completed = self.future.result()
            if self.future_generation == self.generation:
                self.result = completed
            self.future = None
        if (self.result is not None and
                self.result.get("active_car_numbers") != prepared.get("active_car_numbers")):
            # A pit or terminal status changes the projected field immediately.
            self._clear_result()
        if prepared["status"] != "ready_for_baseline":
            held = self._held_result(snapshot, prepared)
            if held is not None:
                return held
            # Invalidates work launched before a disconnect, flag or data gap.
            self._clear_result()
            return {k: v for k, v in prepared.items() if k != "model_input"}
        if self.hold_started_s is not None:
            # Wait for a fresh calculation before presenting live results again.
            self._clear_result()
        revision = (epoch, snapshot["counts"].get("accepted_messages", 0))
        if self.future is None and revision != self.last_submitted:
            self.future_generation = self.generation
            self.last_submitted = revision
            self.future = self.executor.submit(self.runner.project, prepared)
        if self.result is not None:
            lag = prepared["as_of_session_time_s"] - self.result["as_of_session_time_s"]
            if 0 <= lag <= self.maximum_lag_s:
                return {**self.result, "source_lag_s": lag,
                        "updating": self.future is not None,
                        "field_state": prepared["field_state"]}
        return {key: value for key, value in prepared.items() if key != "model_input"} | {
            "status": "calculating", "reason": "Field ready; calculating a current forecast"}

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


def print_forecast(result):
    status = result["status"]
    if status == "unavailable":
        print(result["reason"])
        return
    ready = result.get("readiness", {})
    prefix = f"Forecast car {result.get('selected_car', '?')}"
    if status == "waiting_for_inputs":
        print(f"{prefix}: waiting | {ready.get('ready_cars', 0)}/{ready.get('field_size', 0)} cars ready")
        need_pace = [car for car in ready.get("cars", {}).values()
                     if any("need pace" in reason for reason in car["reasons"])]
        if need_pace:
            minimum = ready["minimum_clean_laps"]
            print(f"Pace warm-up: {len(need_pace)} cars still need {minimum} complete green laps after lap 1.")
            counts = {count: sum(car["clean_laps"] == count for car in need_pace)
                      for count in range(minimum)}
            print("  " + " | ".join(f"{count}/{minimum} laps: {total} cars"
                                    for count, total in counts.items() if total))
        reasons = [reason for reason in result.get("missing", []) if not reason.startswith("Car ")]
        for identifier, car in ready.get("cars", {}).items():
            issues = [reason for reason in car["reasons"] if "need pace" not in reason]
            if "source crossing too old" in issues:
                issues[issues.index("source crossing too old")] = (
                    f"source crossing is {car['source_crossing_age_s']:.1f}s old "
                    f"(limit {car['maximum_source_age_s']:.0f}s)")
            if issues:
                reasons.append(f"Car {identifier}: " + "; ".join(issues))
        for reason in reasons[:4]:
            print("  " + reason)
        if len(reasons) > 4:
            print(f"  Plus {len(reasons) - 4} other input issues (details with --json).")
        return
    if status not in ("baseline_projection", "held_projection"):
        print(f"{prefix}: {result.get('reason', status)}")
        return
    label = "HELD - LAST VALID FORECAST" if status == "held_projection" else "BASELINE"
    print(f"{prefix}: {label} | source t={result['as_of_session_time_s']:.3f}s | "
          f"source lag {result.get('source_lag_s', 0):.1f}s")
    if status == "held_projection":
        hold = result["hold"]
        print(f"Car {hold['car']} crossing is {hold['source_crossing_age_s']:.1f}s old. "
              f"No new calculation; holding for at most {hold['maximum_hold_s']:.0f}s.")
    if result.get("calibration_note"):
        print(result["calibration_note"])
    pit_cars = [car for car in ready.get("excluded_cars", []) if car["status"] in PIT_STATUSES]
    if pit_cars:
        print(f"On-track field only: {len(pit_cars)} pit car(s) omitted. "
              "Projected positions are provisional; pit rejoins are not modeled.")
    print("Horizons are common future times based on 1/2 selected-car laps; gaps are baseline estimates.")
    for scenario in result["scenarios"]:
        if scenario["status"] != "model_projection":
            continue
        print(f"  {scenario['label']} (pit loss {scenario['net_pit_loss_s']:.1f}s)")
        for horizon in scenario["horizons"]:
            ahead = ("no other car" if horizon["car_ahead"] is None else
                     f"#{horizon['car_ahead']} ahead {horizon['gap_ahead_s']:.1f}s")
            behind = ("" if horizon["car_behind"] is None else
                      f" | #{horizon['car_behind']} behind {horizon['gap_behind_s']:.1f}s")
            pack = "nearby traffic" if horizon["traffic_pack"] else "no nearby traffic"
            print(f"    +{horizon['model_laps_ahead']}: P{horizon['position']} "
                  f"class {horizon['class_position'] or '?'} | {ahead}{behind} | {pack}")
    unknown = sum(item["status"] == "needs_configuration" for item in result["scenarios"])
    if unknown:
        print(f"  {unknown} pit scenarios need durations in the pit configuration.")
