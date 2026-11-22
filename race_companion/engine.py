"""Transparent independent constant-pace baseline, implemented for this edition.

This module has no traffic-interaction simulator or proprietary dependency.
"""

from .state import number


def project(model, parameters, selected_car, pit_scenarios=()):
    cars = model["cars"]
    identifiers = [car["number"] for car in cars]
    if (not cars or any(not isinstance(value, str) or not value for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ValueError("Cars require unique nonempty identifiers")
    if selected_car not in identifiers:
        raise ValueError("Selected car is outside the projected field")
    paces = {car["number"]: number(car["lap_time_s"], minimum=.001) for car in cars}
    starts = {car["number"]: number(car["distance_laps"]) for car in cars}
    categories = {car["number"]: car.get("category") for car in cars}
    selected_pace = paces[selected_car]
    close_gap = number(parameters.get("close_gap_s", 3))
    scenarios = [{"id": "stay_out", "label": "Stay out", "status": "configured", "net_pit_loss_s": 0}]
    scenarios.extend(scenario for scenario in pit_scenarios if scenario["id"] != "stay_out")
    results = []
    for scenario in scenarios:
        if scenario["status"] != "configured":
            results.append({"id": scenario["id"], "label": scenario["label"],
                            "status": "needs_configuration", "missing": scenario.get("missing", [])})
            continue
        loss = number(scenario["net_pit_loss_s"])
        horizons = []
        for step in (1, 2):
            elapsed = step * selected_pace
            progress = {identifier: starts[identifier] + max(0, elapsed - (loss if identifier == selected_car else 0)) / paces[identifier]
                        for identifier in identifiers}
            ranking = sorted(identifiers, key=lambda identifier: (-progress[identifier], identifier))
            rivals = [identifier for identifier in identifiers if identifier != selected_car]
            phase = progress[selected_car] % 1
            ahead = min(rivals, key=lambda identifier: ((progress[identifier] % 1 - phase) % 1, identifier)) if rivals else None
            behind = min(rivals, key=lambda identifier: ((phase - progress[identifier] % 1) % 1, identifier)) if rivals else None
            gap_ahead = None if ahead is None else ((progress[ahead] % 1 - phase) % 1) * selected_pace
            gap_behind = None if behind is None else ((phase - progress[behind] % 1) % 1) * selected_pace
            class_position = None
            if all(categories.values()):
                own_class = [identifier for identifier in ranking if categories[identifier] == categories[selected_car]]
                class_position = own_class.index(selected_car) + 1
            horizons.append({"model_laps_ahead": step, "horizon_s": elapsed,
                "forecast_session_time_s": number(model["source_time_s"]) + elapsed,
                "position": ranking.index(selected_car) + 1, "class_position": class_position,
                "car_ahead": ahead, "gap_ahead_s": gap_ahead, "car_behind": behind,
                "gap_behind_s": gap_behind, "traffic_pack": gap_ahead is not None and gap_ahead <= close_gap,
                "distance_laps": progress[selected_car]})
        results.append({"id": scenario["id"], "label": scenario["label"], "status": "model_projection",
                        "net_pit_loss_s": loss, "pit_loss_source": scenario.get("pit_loss_source", "configuration"),
                        "horizons": horizons})
    return {"status": "baseline_projection", "selected_car": selected_car,
            "model": "independent_constant_pace", "scenarios": results,
            "horizon_definition": "Common future times, one/two selected-car pace laps ahead.",
            "assumptions": model.get("assumptions", [])}
