# Race Engineer Companion — Public Edition

A Python desktop timing dashboard for exploring pit-stop scenarios in multiclass racing. It combines a streaming timing pipeline, measured traffic display, editable pit losses and a deliberately simple constant-pace baseline.

**Run it without HH Timing, an account, race data or third-party Python packages.** The included demo generates fictional timing events and feeds them through the same validation, state and forecasting path as a live connection.

## Quick start

Use Python 3.12 or newer, with Tkinter available. From this directory on Windows:

```powershell
py -3.12 -m race_companion gui
```

Click **Run synthetic demo**. It plays at 20x source time and lasts about 44 seconds. Baseline results appear after the cars have completed the pace warm-up, approximately 17 seconds into playback. Choose **Replay recording** with the bundled example and **Instant** for immediate final results.

For an immediate command-line demonstration:

```powershell
py -3.12 -m race_companion demo
```

On other platforms, use `python3` instead. Some Linux installations require the OS Tkinter package for the GUI; the command-line demo and tests do not require a display.

## Features

- Newline-JSON TCP ingestion, reconnect handling and raw recording.
- Validated timing state with bounded history and source/receipt timestamps.
- Current classification, physical neighbours and measurement-age indicators.
- Independent constant-pace projections at one and two selected-car laps ahead.
- Side-by-side stay-out, tyre, refuel and custom pit scenarios.
- An engineer pit-time editor with file defaults, overrides and reset.
- Background calculation with bounded pending work and invalidation of obsolete results.
- Deterministic synthetic data and saved-recording replay.
- Automated regression tests for the baseline and application pipeline.

## What the baseline means

The public predictor is intentionally transparent: each car continues at the median of its recent accepted green lap times. Starting progress is estimated linearly from its latest crossing. Cars advance independently; there are no passing decisions, traffic-induced pace changes or driver behaviour rules.

Both horizons are **common future timestamps**: the snapshot time plus one or two nominal laps of the selected car. A pit scenario subtracts a configurable duration from the selected car's available driving time. This is a simple delay comparison, not a pit-entry/service/exit trajectory or a recommendation of the optimal lap to pit.

Forecast classification is sorted by estimated total progress. Forecast physical neighbours use progress around the lap, with gap equivalents expressed at the selected car's pace. These estimates are separate from the actual last measured timing-line gaps displayed in the timing table. **Close** means the predicted nearest-ahead gap falls within the configurable warning threshold; it does not imply simulated interaction.

Pit and terminal-status cars are excluded from the forecast. Positions cover the reported on-track field; rival pit strategies and rejoins are not predicted. Non-green conditions, missing pace or stale/missing required measurements pause the calculation. No quantified race-accuracy claim is made.

## Adjusting pit assumptions

Click **Adjust pit times...**. Values are **total time lost against staying on track**, including pit-lane and service loss. They are not stationary service durations or raw pit-in/out intervals.

Use the 0.5-second arrows or type a nonnegative duration, then **Apply and recalculate**. Overrides are marked with `*`; **Reset to file defaults** restores the configured totals. Each scenario is independent. Changes last for the app/event session and are not written into config files or recorded timing data. A completed replay can be recalculated, including for another selected car.

All bundled pit durations are fictional. `config/pit_scenarios.json` also demonstrates sequential service stages and overlapping services within a stage. This arrangement is configurable rather than a claim about a championship's rules.

## Replay and recording

```powershell
py -3.12 -m race_companion replay examples/synthetic_race.jsonl
py -3.12 -m race_companion inspect examples/synthetic_race.jsonl
py -3.12 -m race_companion demo --json
py -3.12 -m race_companion demo --write recordings/my_synthetic_race.jsonl
```

Recording uses exclusive creation to protect existing files. Generated recordings are ignored by Git. The example is generated from `race_companion/demo.py`; it contains no captured race or private-model results.

## Optional live connection

The app consumes the documented application message format over localhost TCP; it does not connect directly to a timing provider. A compatible external bridge is required for HH Timing integration. No HH DLLs, SDKs or bridge implementation are included. See [the feed contract](docs/FEED_FORMAT.md).

To test the TCP path using only this repository, start a synthetic server in one terminal:

```powershell
py -3.12 -m race_companion demo --serve
```

Then choose **Start live + record** in the GUI, or run in a second terminal:

```powershell
py -3.12 -m race_companion live
```

The demo server serves one client and exits after the fictional race. For a different compatible live event, create your own event profile with the correct car roster and timing-line fractions; the bundled profile is for the fictional demo only.

## Tests and architecture

```powershell
py -3.12 -m unittest discover -s tests -v
```

See [architecture](docs/ARCHITECTURE.md) for module responsibilities and [verification](docs/VERIFICATION.md) for the checks performed. Python's standard library is the only runtime requirement. A GUI needs an interactive desktop; the tests are headless.

This portfolio edition demonstrates the application architecture and a basic extrapolation method. Advanced traffic modelling is outside its scope.
