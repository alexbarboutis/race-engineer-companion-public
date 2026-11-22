from copy import deepcopy
import unittest

from race_companion.engine import project


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.model = {"source_time_s": 500, "cars": [
            {"number": "A", "category": "Prototype", "distance_laps": 2.8, "lap_time_s": 100},
            {"number": "B", "category": "Prototype", "distance_laps": 2.9, "lap_time_s": 110},
            {"number": "C", "category": "GT", "distance_laps": 1.7, "lap_time_s": 120}]}

    def test_common_future_times_and_independent_progress(self):
        result = project(self.model, {}, "A")["scenarios"][0]["horizons"]
        self.assertEqual([row["forecast_session_time_s"] for row in result], [600, 700])
        self.assertAlmostEqual(result[0]["distance_laps"], 3.8)
        self.assertEqual([row["position"] for row in result], [2, 1])
        self.assertEqual([row["class_position"] for row in result], [2, 1])

    def test_physical_neighbour_is_separate_from_classification(self):
        row = project(self.model, {}, "A")["scenarios"][0]["horizons"][1]
        self.assertEqual(row["position"], 1)
        self.assertEqual(row["car_ahead"], "C")
        self.assertGreaterEqual(row["gap_ahead_s"], 0)
        self.assertLess(row["gap_ahead_s"], 100)

    def test_pit_delay_is_applied_once_and_inputs_are_unchanged(self):
        original = deepcopy(self.model)
        scenario = {"id": "stop", "label": "Stop", "status": "configured", "net_pit_loss_s": 30}
        result = project(self.model, {}, "A", [scenario])["scenarios"]
        for baseline, stop in zip(result[0]["horizons"], result[1]["horizons"]):
            self.assertAlmostEqual(baseline["distance_laps"] - stop["distance_laps"], .3)
        self.assertEqual(self.model, original)

    def test_long_delay_does_not_move_a_car_backwards(self):
        scenario = {"id": "stop", "label": "Stop", "status": "configured", "net_pit_loss_s": 300}
        horizons = project(self.model, {}, "A", [scenario])["scenarios"][1]["horizons"]
        self.assertEqual([row["distance_laps"] for row in horizons], [2.8, 2.8])

    def test_warning_threshold_does_not_change_trajectories(self):
        near = project(self.model, {"close_gap_s": 100}, "A")
        far = project(self.model, {"close_gap_s": 0}, "A")
        for left, right in zip(near["scenarios"][0]["horizons"], far["scenarios"][0]["horizons"]):
            self.assertNotEqual(left.pop("traffic_pack"), right.pop("traffic_pack"))
            self.assertEqual(left, right)

    def test_missing_class_metadata_does_not_invent_a_class_rank(self):
        self.model["cars"][1]["category"] = None
        result = project(self.model, {}, "A")["scenarios"][0]["horizons"]
        self.assertIsNone(result[0]["class_position"])

    def test_single_car_has_no_physical_neighbours(self):
        self.model["cars"] = self.model["cars"][:1]
        row = project(self.model, {}, "A")["scenarios"][0]["horizons"][0]
        self.assertEqual(row["position"], 1)
        self.assertIsNone(row["car_ahead"])
        self.assertIsNone(row["car_behind"])

    def test_invalid_input_and_negative_pit_delay_are_rejected(self):
        with self.assertRaises(ValueError):
            project(self.model, {}, "unknown")
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                project(self.model, {}, "A", [{"id": "stop", "label": "Stop", "status": "configured", "net_pit_loss_s": value}])


if __name__ == "__main__":
    unittest.main()
