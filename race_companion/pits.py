"""Configurable net pit losses for independent what-if scenarios.

Stages happen sequentially. Services within each stage happen concurrently.
This is a modelling input, not an assertion about any championship's rules.
"""

import json
from copy import deepcopy
from pathlib import Path

from .state import number


def evaluate_scenario(scenario):
    identifier = scenario["id"]
    label = scenario["label"]
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("Scenario requires an id")
    if not isinstance(label, str) or not label:
        raise ValueError("Scenario requires a label")
    stay_out = scenario.get("stay_out", False)
    if not isinstance(stay_out, bool):
        raise ValueError("stay_out must be true or false")
    if stay_out:
        if scenario.get("pit_lane_delta_s") is not None or scenario.get("service_stages"):
            raise ValueError("Stay-out scenario cannot include pit transit or service")
        return {"id": identifier, "label": label, "status": "configured",
                "net_pit_loss_s": 0.0}

    missing = []
    lane = scenario.get("pit_lane_delta_s")
    if lane is None:
        missing.append("pit_lane_delta_s")
        lane = 0.0
    else:
        lane = number(lane)
    stages = scenario.get("service_stages")
    if not isinstance(stages, list):
        raise ValueError("service_stages must be a list; [] means no stationary service")
    service_total = 0.0
    for stage_index, stage in enumerate(stages):
        if not isinstance(stage, list) or not stage:
            raise ValueError("Each service stage must be a nonempty list")
        durations = []
        for service in stage:
            if not isinstance(service, dict) or not isinstance(service.get("name"), str):
                raise ValueError("Every service needs a name")
            duration = service.get("duration_s")
            if duration is None:
                missing.append(f"stage[{stage_index}].{service['name']}.duration_s")
            else:
                durations.append(number(duration))
        service_total += max(durations, default=0.0)
    return {
        "id": identifier, "label": label,
        "tyre_compound": scenario.get("tyre_compound"),
        "status": "needs_configuration" if missing else "configured",
        "missing": missing,
        "net_pit_loss_s": None if missing else lane + service_total,
        "stationary_service_s": None if missing else service_total,
    }


def load_scenarios(path):
    with Path(path).open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data.get("scenarios"), list) or not data["scenarios"]:
        raise ValueError("Configuration requires a nonempty scenarios list")
    results = [evaluate_scenario(item) for item in data["scenarios"]]
    ids = [item["id"] for item in results]
    if len(ids) != len(set(ids)):
        raise ValueError("Scenario ids must be unique")
    return results


def apply_pit_overrides(scenarios, overrides):
    """Apply engineer-entered total net losses to detached evaluated scenarios."""
    known = {scenario["id"] for scenario in scenarios if scenario["id"] != "stay_out"}
    if set(overrides) - known:
        raise ValueError("Pit adjustment refers to an unknown or stay-out scenario")
    durations = {identifier: number(value) for identifier, value in overrides.items()}
    adjusted = deepcopy(scenarios)
    for scenario in adjusted:
        if scenario["id"] in durations:
            scenario.update(status="configured", net_pit_loss_s=durations[scenario["id"]],
                            pit_loss_source="engineer", missing=[])
            # The total override does not imply a measured stationary duration.
            scenario["stationary_service_s"] = None
    return adjusted
