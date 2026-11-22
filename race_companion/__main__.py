"""Public command line: demo, desktop GUI, live recording and replay."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import socket
import sys

from .dashboard_session import PROJECT_ROOT
from .demo import demo_records
from .diagnostics import inspect_recording
from .feed import Recorder, live_records, replay_records
from .forecasting import ForecastRunner, LiveForecaster, print_forecast, read_event
from .state import TimingStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Public timing dashboard with a constant-pace baseline")
    sub = parser.add_subparsers(dest="command", required=True)
    gui = sub.add_parser("gui", help="Desktop dashboard, including a synthetic demo button")
    gui.add_argument("--event", type=Path, default=PROJECT_ROOT / "config/demo.json")
    gui.add_argument("--car", default="101")
    demo = sub.add_parser("demo", help="Run fictional timing data through the complete pipeline")
    demo.add_argument("--write", type=Path, help="Save synthetic envelopes to a new JSONL file")
    demo.add_argument("--serve", action="store_true", help="Serve the synthetic feed over localhost TCP")
    demo.add_argument("--port", type=int, default=8765)
    live = sub.add_parser("live", help="Connect to a compatible newline-JSON timing bridge")
    live.add_argument("--host", default="127.0.0.1")
    live.add_argument("--port", type=int, default=8765)
    live.add_argument("--record", type=Path)
    replay = sub.add_parser("replay", help="Replay a compatible recording")
    replay.add_argument("recording", type=Path)
    replay.add_argument("--speed", type=float, default=0, help="0=instant, 1=recorded receipt cadence")
    inspect = sub.add_parser("inspect", help="Inspect message coverage and ingestion counts")
    inspect.add_argument("recording", type=Path)
    for command in (demo, live, replay):
        command.add_argument("--event", type=Path, default=PROJECT_ROOT / "config/demo.json")
        command.add_argument("--car", default="101")
        command.add_argument("--json", action="store_true", help="Print the final snapshot as JSON")
        command.add_argument("--pits", type=Path)
    args = parser.parse_args(argv)
    recorder = forecaster = records = None
    try:
        if args.command == "gui":
            from .gui import run_gui
            return run_gui(args.event, args.car)
        if args.command == "inspect":
            print(json.dumps(inspect_recording(args.recording), indent=2, allow_nan=False))
            return 0
        if hasattr(args, "port") and not 1 <= args.port <= 65535:
            parser.error("Port must be from 1 to 65535")
        if args.command == "replay" and (not math.isfinite(args.speed) or args.speed < 0):
            parser.error("Replay speed must be finite and nonnegative")
        if args.command == "demo" and args.serve:
            with socket.socket() as server:
                server.bind(("127.0.0.1", args.port))
                server.listen(1)
                print(f"Synthetic feed listening on 127.0.0.1:{args.port}", flush=True)
                connection, _ = server.accept()
                with connection:
                    for record in demo_records(paced=True):
                        if record["kind"] == "message":
                            connection.sendall((record["raw"] + "\n").encode("utf-8"))
            return 0
        profile, metadata = read_event(args.event)
        runner = ForecastRunner.from_args(args, profile)
        store = TimingStore(metadata=metadata)
        if args.command == "live":
            path = args.record or PROJECT_ROOT / "recordings" / datetime.now(timezone.utc).strftime("timing_%Y%m%d_%H%M%S_%f.jsonl")
            recorder = Recorder(path)
            print(f"Recording to {path}", file=sys.stderr)
            records = live_records(args.host, args.port)
            forecaster = LiveForecaster(runner)
        elif args.command == "replay":
            records = replay_records(args.recording, speed=args.speed)
        else:
            records = demo_records()
            if args.write:
                recorder = Recorder(args.write)
        now = 0
        last_display = -float("inf")
        for record in records:
            if recorder:
                recorder.write(record)
            store.consume(record)
            now = record["received_elapsed_s"]
            if forecaster and now - last_display >= 1:
                snapshot = store.snapshot(now, stale_after_s=150)
                snapshot["prediction"] = forecaster.update(snapshot)
                if args.json:
                    print(json.dumps(snapshot, allow_nan=False))
                else:
                    print_forecast(snapshot["prediction"])
                last_display = now
        snapshot = store.snapshot(now, stale_after_s=150)
        snapshot["prediction"] = runner.forecast(snapshot)
        if args.json:
            print(json.dumps(snapshot, indent=2, allow_nan=False))
        else:
            print(f"{len(snapshot['cars'])} cars; {store.counts['accepted_messages']} accepted timing messages")
            print_forecast(snapshot["prediction"])
        return 0
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if records is not None and hasattr(records, "close"):
            records.close()
        if forecaster:
            forecaster.close()
        if recorder:
            recorder.close()


if __name__ == "__main__":
    raise SystemExit(main())
