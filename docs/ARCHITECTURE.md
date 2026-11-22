# Architecture

## Processing path

```text
TCP feed / synthetic generator / saved recording
                    |
               raw envelope
                    |
            TimingStore validation
                    |
             detached snapshot
                    |
        readiness + simple pace estimate
                    |
       independent constant-pace baseline
                    |
          CLI / Tkinter scenario table
```

## Responsibilities

| Module | Responsibility |
| --- | --- |
| `feed.py` | TCP framing, reconnects, exclusive recording and replay validation |
| `state.py` | Normalized car state, bounded histories and observation timestamps |
| `physical_field.py` | Physical-field schema, reciprocal-pair validation and measurement ages |
| `estimator.py` | Active-field readiness, median green-lap pace and linear starting progress |
| `engine.py` | Self-contained independent constant-pace calculations and simple pit-delay comparisons |
| `pits.py` | Configured service-stage totals and detached engineer overrides |
| `forecasting.py` | Shared runner, freshness checks and a single coalescing forecast worker |
| `dashboard_session.py` | Background feed session, selected car, settings revisions and UI publication |
| `gui.py` | Tkinter timing display, sortable columns, scenario table and pit editor |
| `demo.py` | Deterministic fictional event generation and interruptible paced playback |

## Concurrency and state

The feed/session thread owns timing state. Only detached snapshots cross into forecasting or the UI. Tk widgets are updated from the Tk thread. One calculation can run while new snapshots replace the next candidate, avoiding an unbounded job queue.

Connection epochs invalidate old work. Source-time lag prevents a completed calculation from being shown as current when it is too far behind. Pit changes rebuild the prediction runner, retain the feed/history and increment a settings revision; results for older settings cannot replace current ones. An ended replay can be recalculated from its final snapshot.

## Time and observation limits

Source session time and local receipt time have separate roles. The source clock stays at the latest received source timestamp. Crossing ages and pair-measurement ages are retained. The UI labels physical observations independently of readiness to forecast.

The baseline's estimated future progress does not enforce consistency with every asynchronous measured gap. Its purpose is a readable demonstration of the application pipeline. The gap display remains a display of last measurements rather than a continuously observed track position.

## Extension point

`ForecastRunner` accepts a prediction callable. Any alternate implementation must accept the prepared baseline state, public options, selected car and evaluated pit scenarios, and return the documented scenario/horizon fields consumed by the UI. This edition ships with only the independent constant-pace implementation and requires no external prediction package.
