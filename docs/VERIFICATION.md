# Verification

Checked on 22 November 2026: all 18 headless tests passed on Python 3.12. A withdrawn Tk window check also exercised six timing rows, six scenario rows, baseline labels, selecting another car, applying/resetting pit overrides, and stopping the synthetic demo.

The public edition has a dedicated headless test suite, independent of other projects and their reference outputs.

Coverage includes:

- Common future times and independent constant-pace progress.
- Physical neighbours versus classification, including lap wrap-around.
- Once-per-scenario pit delay and very long delay handling.
- No input mutation, unknown class ranks and single-car fields.
- Invalid numerical input and invalid pit settings.
- Deterministic synthetic event generation and schema acceptance.
- Pace warm-up, disconnection, stalled clocks and non-green conditions.
- Missing/stale physical measurements and reciprocal sample validation.
- Recording/replay round-trip and recalculation of completed replays.
- Preserving the live connection and recorder when pit settings change.
- Interruptible synthetic playback.

Run `python -m unittest discover -s tests -v` from the repository root. These tests verify application behaviour and the stated simple baseline; they do not establish prediction accuracy for an actual race.
