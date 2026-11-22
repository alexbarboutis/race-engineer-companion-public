"""Background live/replay session for the desktop dashboard.

Only detached snapshots cross to the UI thread. Tk widgets never run here.
"""

from datetime import datetime, timezone
from pathlib import Path
from queue import SimpleQueue
import threading
import time
from types import SimpleNamespace

from .feed import Recorder, live_records, replay_records
from .forecasting import ForecastRunner, LiveForecaster, read_event
from .pits import apply_pit_overrides
from .state import TimingStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DashboardSession:
    def __init__(self, *, mode, event_path, selected_car, recording_path=None,
                 replay_speed=0, host="127.0.0.1", port=8765, pit_overrides=None):
        if mode not in ("live", "replay", "demo"):
            raise ValueError("Dashboard mode must be live, replay or demo")
        if not selected_car:
            raise ValueError("Select a car number")
        if mode == "replay" and recording_path is None:
            raise ValueError("Select a recording to replay")
        self.mode = mode
        self.event_path = Path(event_path)
        self._selected_car = selected_car
        self.recording_path = Path(recording_path) if recording_path else None
        self.replay_speed = replay_speed
        self.host = host
        self.port = port
        self.events = SimpleQueue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._revision = 0
        self._snapshot = None
        self._thread = None
        self._pit_overrides = dict(pit_overrides or {})
        self._pit_settings_revision = 0
        self._base_runner = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def selected_car(self):
        with self._lock:
            return self._selected_car

    def select_car(self, car_number):
        if not car_number:
            raise ValueError("Select a car number")
        with self._lock:
            self._selected_car = car_number

    @property
    def pit_settings_revision(self):
        with self._lock:
            return self._pit_settings_revision

    def set_pit_overrides(self, overrides):
        overrides = dict(overrides)
        with self._lock:
            if self._base_runner is not None:
                apply_pit_overrides(self._base_runner.scenarios, overrides)
            if overrides == self._pit_overrides:
                return
            self._pit_overrides = overrides
            self._pit_settings_revision += 1
        self.refresh_forecast()

    def _configured_runner(self):
        with self._lock:
            selected_car = self._selected_car
            revision = self._pit_settings_revision
            overrides = dict(self._pit_overrides)
        base = self._base_runner
        return ForecastRunner(base.profile, base.parameters, selected_car,
                              apply_pit_overrides(base.scenarios, overrides),
                              projector=base.projector), revision

    def refresh_forecast(self):
        """Recalculate an ended replay from its retained snapshot, without replaying."""
        if self.running or self._base_runner is None:
            return
        _, snapshot = self.latest()
        if snapshot is not None:
            runner, revision = self._configured_runner()
            refreshed = dict(snapshot)
            refreshed["prediction"] = runner.forecast(snapshot)
            refreshed["prediction"]["pit_settings_revision"] = revision
            self._publish(refreshed)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Session has already started")
        self._thread = threading.Thread(target=self._run, name="dashboard-feed", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def latest(self):
        with self._lock:
            return self._revision, self._snapshot

    def _publish(self, snapshot):
        with self._lock:
            if snapshot["prediction"].get("pit_settings_revision", 0) != self._pit_settings_revision:
                return
            self._revision += 1
            self._snapshot = snapshot

    def _run(self):
        recorder = None
        forecaster = None
        runner = None
        store = None
        records = None
        now = 0.0
        last_display_wall = -float("inf")
        try:
            profile, metadata = read_event(self.event_path)
            if profile is None:
                raise ValueError("Choose an event profile")
            args = SimpleNamespace(event=self.event_path, parameters=None,
                                   pits=None, car=self.selected_car)
            self._base_runner = ForecastRunner.from_args(args, profile)
            runner, settings_revision = self._configured_runner()
            store = TimingStore(metadata=metadata)
            forecaster = LiveForecaster(runner)
            if self.mode == "live":
                path = self.recording_path or (PROJECT_ROOT / "recordings" /
                    datetime.now(timezone.utc).strftime("hh_%Y%m%d_%H%M%S_%f.jsonl"))
                recorder = Recorder(path)
                self.events.put(("started", f"Recording to {path}"))
                records = live_records(self.host, self.port)
            elif self.mode == "demo":
                from .demo import demo_records
                self.events.put(("started", "Synthetic demo: fictional cars and timing at 20x source time"))
                records = demo_records(paced=True, stop_event=self._stop)
            else:
                self.events.put(("started", f"Replaying {self.recording_path}"))
                records = replay_records(self.recording_path, speed=self.replay_speed)
            for record in records:
                if self._stop.is_set():
                    break
                if recorder:
                    recorder.write(record)
                store.consume(record)
                now = record["received_elapsed_s"]
                if (self.selected_car != runner.selected_car or
                        settings_revision != self.pit_settings_revision):
                    forecaster.close()
                    runner, settings_revision = self._configured_runner()
                    forecaster = LiveForecaster(runner)
                    last_display_wall = -float("inf")
                wall = time.monotonic()
                if record["kind"] == "connection" or wall - last_display_wall >= 0.5:
                    snapshot = store.snapshot(now, stale_after_s=150)
                    snapshot["prediction"] = forecaster.update(snapshot)
                    snapshot["prediction"]["pit_settings_revision"] = settings_revision
                    self._publish(snapshot)
                    last_display_wall = wall
            # EOF and Stop show the exact final state, even for instant replay.
            snapshot = store.snapshot(now, stale_after_s=150)
            runner, settings_revision = self._configured_runner()
            snapshot["prediction"] = runner.forecast(snapshot)
            snapshot["prediction"]["pit_settings_revision"] = settings_revision
            self._publish(snapshot)
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            try:
                if records is not None and hasattr(records, "close"):
                    records.close()
                if forecaster:
                    forecaster.close()
            finally:
                if recorder:
                    recorder.close()
                self.events.put(("finished", "Recording stopped" if self.mode == "live" else "Replay finished"))
