# Application timing feed

The TCP consumer accepts UTF-8 JSON objects separated by newline characters. This is the application's normalized input contract, not a claim that a timing provider exposes this protocol directly. The included demo server implements it without external software.

Car numbers are exact strings. Preserve leading zeroes. Durations and source session timestamps are seconds; optional speed readings must explicitly use `km/h`.

## Basic messages

```json
{"type":"car","car":"101","category":"Prototype","team":"Demo team"}
{"type":"driver","car":"101","driver":"Example Driver"}
{"type":"status","car":"101","status":"OnTrackRunning","session_time":100}
{"type":"sector","car":"101","lap":2,"sector":"S1","sector_time":30.4,"sector_exit_time":130.4,"track_status":"Green"}
{"type":"lap","car":"101","lap":2,"lap_time":95,"elapsed_time":195,"position":1,"class_position":1,"in_lap":false,"out_lap":false,"track_status":"Green"}
{"type":"speed","car":"101","lap":2,"trap":"Demo trap","unit":"km/h","speed_kmh":280}
```

`S1`, `S2` and `S3` are supported. Lap 1 and laps not explicitly marked green and outside pit-in/out are excluded from the public baseline's pace sample. Speed readings are display information and are not required by the baseline.

## Physical field

`physical_field` is a complete snapshot with `schema_version: 1`, a `session_epoch` string, increasing `sequence`, source `session_time_s`, `gap_method: "last_measured_crossing"`, optional `reference_car`, `physical_order` and `cars`.

Each car row has:

- `car`, `active`, `status`, `track_status`, `lap`, `sector` and `crossing_session_time_s`.
- One-based `physical_order` for active cars; null for inactive cars.
- `car_ahead` and `car_behind` matching the circular order.
- `gap_ahead` and `gap_behind`: a measured sample or null when unavailable.

A sample contains:

```json
{
  "from_car": "202",
  "to_car": "101",
  "gap_s": 5.0,
  "measured_at_session_time_s": 200.0,
  "ahead_crossing_session_time_s": 195.0,
  "timing_line": "S3",
  "is_start_finish": true
}
```

The same rear-to-front pair sample appears as the rear car's `gap_ahead` and front car's `gap_behind`. Pair direction, timestamp subtraction, reciprocal equality and the circular neighbour topology are validated. A display reference anchors ordering only; it does not define gap arithmetic. A changed session epoch clears prior history.

For a complete generated example, inspect `examples/synthetic_race.jsonl` or `demo.py`. The example is reproducible and fictional.

## Recorded envelopes

Recordings wrap raw messages with `schema_version: 1`, `recording_id`, increasing `sequence`, monotonic `received_elapsed_s`, `received_utc` and `kind: "message"`. The `raw` field contains the original JSON line as a string. Connection envelopes use `kind: "connection"` and `state`. Replay validates identity, sequence and monotonic receipt time before ingestion.
