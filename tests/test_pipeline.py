from copy import deepcopy
import json
from pathlib import Path
from threading import Event
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from race_companion.dashboard_session import DashboardSession
from race_companion.demo import demo_records, synthetic_records
from race_companion.feed import Recorder, replay_records
from race_companion.forecasting import ForecastRunner, read_event
from race_companion.physical_field import validate_field
from race_companion.pits import apply_pit_overrides, evaluate_scenario, load_scenarios
from race_companion.state import TimingStore


ROOT = Path(__file__).resolve().parents[1]


def final_store():
    store = TimingStore()
    now = 0
    for record in demo_records():
        store.consume(record)
        now = record["received_elapsed_s"]
    return store, now


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.profile, _ = read_event(ROOT / "config/demo.json")
        args = SimpleNamespace(event=ROOT / "config/demo.json", car="101", pits=None)
        self.runner = ForecastRunner.from_args(args, self.profile)

    def test_demo_is_deterministic_and_every_message_is_valid(self):
        self.assertEqual(synthetic_records(), synthetic_records())
        store, now = final_store()
        self.assertEqual(store.counts["invalid_messages"], 0)
        self.assertEqual(len(store.cars), 6)
        self.assertEqual(store.counts["accepted_messages"], store.counts["received_lines"])
        result = self.runner.forecast(store.snapshot(now))
        self.assertEqual(result["status"], "baseline_projection")
        self.assertEqual(len(result["scenarios"]), 6)

    def test_warmup_waits_for_observed_pace(self):
        store = TimingStore()
        records = synthetic_records(laps=1)
        for record in records:
            store.consume(record)
        result = self.runner.forecast(store.snapshot(records[-1]["received_elapsed_s"]))
        self.assertEqual(result["status"], "waiting_for_inputs")
        self.assertTrue(any("need pace" in reason for reason in result["missing"]))

    def test_disconnect_stalled_clock_and_non_green_pause_prediction(self):
        store, now = final_store()
        snapshot = store.snapshot(now)
        for change in ("connection", "clock", "flag"):
            with self.subTest(change=change):
                altered = deepcopy(snapshot)
                if change == "connection":
                    altered["connected"] = False
                elif change == "clock":
                    altered["source_clock_age_s"] = 100
                else:
                    altered["cars"][0]["track_status"] = "SafetyCar"
                self.assertEqual(self.runner.forecast(altered)["status"], "waiting_for_inputs")

    def test_missing_or_stale_measured_gaps_pause_prediction(self):
        store, now = final_store()
        for change in ("missing", "stale"):
            with self.subTest(change=change):
                snapshot = store.snapshot(now)
                row = snapshot["physical_field"]["cars"][0]
                if change == "missing":
                    row["gap_ahead"] = None
                else:
                    row["gap_ahead"]["source_age_s"] = 151
                self.assertEqual(self.runner.forecast(snapshot)["status"], "waiting_for_inputs")

    def test_measured_field_rejects_conflicting_reciprocal_samples(self):
        record = next(record for record in reversed(synthetic_records())
                      if record.get("kind") == "message" and json.loads(record["raw"])["type"] == "physical_field")
        message = json.loads(record["raw"])
        message["cars"][0]["gap_ahead"]["gap_s"] += 1
        with self.assertRaises(ValueError):
            validate_field(message)

    def test_invalid_messages_do_not_partially_change_car_state(self):
        store = TimingStore()
        store.consume({"kind": "message", "received_elapsed_s": 1,
                       "raw": json.dumps({"type": "lap", "car": "bad", "lap": -1, "driver": "bad"})})
        self.assertEqual(store.counts["invalid_messages"], 1)
        self.assertNotIn("bad", store.cars)

    def test_pit_totals_support_parallel_services_and_engineer_overrides(self):
        parallel = evaluate_scenario({"id": "parallel", "label": "Parallel", "pit_lane_delta_s": 10,
            "service_stages": [[{"name": "service A", "duration_s": 20},
                                {"name": "service B", "duration_s": 30}]]})
        self.assertEqual(parallel["net_pit_loss_s"], 40)
        defaults = load_scenarios(ROOT / "config/pit_scenarios.json")
        original = deepcopy(defaults)
        adjusted = apply_pit_overrides(defaults, {"fuel": 42.5, "custom": 48})
        by_id = {row["id"]: row for row in adjusted}
        self.assertEqual(by_id["fuel"]["net_pit_loss_s"], 42.5)
        self.assertEqual(by_id["custom"]["status"], "configured")
        self.assertEqual(defaults, original)
        self.assertEqual(apply_pit_overrides(defaults, {}), original)
        for overrides in ({"fuel": -1}, {"stay_out": 1}, {"unknown": 3}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                apply_pit_overrides(defaults, overrides)

    def test_recording_round_trip_and_ended_replay_recalculation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            recorder = Recorder(path)
            try:
                for record in demo_records():
                    recorder.write(record)
            finally:
                recorder.close()
            self.assertEqual(list(replay_records(path)), synthetic_records())
            session = DashboardSession(mode="replay", event_path=ROOT / "config/demo.json",
                                       selected_car="101", recording_path=path)
            session.start()
            session._thread.join(5)
            self.assertFalse(session.running)
            before = session.latest()[1]["prediction"]["scenarios"]
            self.assertEqual(session.latest()[1]["prediction"]["status"], "baseline_projection")
            session.set_pit_overrides({"fuel": 20, "custom": 49})
            after = {row["id"]: row for row in session.latest()[1]["prediction"]["scenarios"]}
            self.assertEqual(after["fuel"]["net_pit_loss_s"], 20)
            self.assertEqual(after["custom"]["status"], "model_projection")
            session.set_pit_overrides({})
            self.assertEqual(session.latest()[1]["prediction"]["scenarios"], before)

    def test_live_pit_adjustment_preserves_connection_and_recording(self):
        ready, release = Event(), Event()

        def stream(*_args):
            yield {"kind": "connection", "state": "connected", "received_elapsed_s": 0}
            ready.set()
            release.wait(3)
            yield {"kind": "tick", "received_elapsed_s": 1}

        session = DashboardSession(mode="live", event_path=ROOT / "config/demo.json", selected_car="101")
        with patch("race_companion.dashboard_session.live_records", side_effect=stream) as feed, \
                patch("race_companion.dashboard_session.Recorder") as recorder:
            session.start()
            self.assertTrue(ready.wait(2))
            session.set_pit_overrides({"fuel": 50})
            release.set()
            session._thread.join(5)
        feed.assert_called_once()
        self.assertEqual(recorder.return_value.write.call_count, 2)
        self.assertEqual(session.latest()[1]["connection_epoch"], 1)
        self.assertEqual(session.latest()[1]["prediction"]["pit_settings_revision"], 1)

    def test_demo_stop_is_interruptible(self):
        stop = Event()
        stream = demo_records(paced=True, stop_event=stop)
        next(stream)
        stop.set()
        self.assertEqual(list(stream), [])


if __name__ == "__main__":
    unittest.main()
