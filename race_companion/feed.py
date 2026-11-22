"""TCP newline JSON transport, raw recording, and deterministic replay."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import socket
import time
import uuid


MAX_LINE_BYTES = 1024 * 1024


class RecordClock:
    def __init__(self):
        self.started = time.monotonic()
        self.recording_id = str(uuid.uuid4())
        self.sequence = 0

    def make(self, kind, **fields):
        self.sequence += 1
        return {
            "schema_version": 1,
            "recording_id": self.recording_id,
            "sequence": self.sequence,
            "received_elapsed_s": time.monotonic() - self.started,
            "received_utc": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **fields,
        }


class Recorder:
    """Exclusive create prevents accidentally replacing a race capture."""

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("x", encoding="utf-8")

    def write(self, record):
        if record["kind"] == "tick":
            return
        self.file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


def live_records(host="127.0.0.1", port=8765, *, reconnect=True):
    clock = RecordClock()
    retry_s = 1.0
    yield clock.make("connection", state="connecting", detail=f"{host}:{port}")
    while True:
        try:
            with socket.create_connection((host, port), timeout=3.0) as sock:
                sock.settimeout(1.0)
                yield clock.make("connection", state="connected", detail=f"{host}:{port}")
                buffer = b""
                while True:
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        yield clock.make("tick")
                        continue
                    if not chunk:
                        if buffer:
                            yield clock.make("transport_error", detail="Discarded incomplete final line")
                        raise ConnectionError("Timing feed closed the connection")
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if len(line) > MAX_LINE_BYTES:
                            raise ConnectionError("Timing line exceeds 1 MiB")
                        if not line.strip():
                            continue
                        try:
                            raw = line.decode("utf-8")
                        except UnicodeDecodeError:
                            yield clock.make("transport_error", detail="Invalid UTF-8 line", raw_hex=line.hex())
                            continue
                        retry_s = 1.0
                        yield clock.make("message", raw=raw)
                    if len(buffer) > MAX_LINE_BYTES:
                        raise ConnectionError("Unterminated timing line exceeds 1 MiB")
        except OSError as exc:
            yield clock.make("connection", state="disconnected", detail=str(exc))
            if not reconnect:
                return
            deadline = time.monotonic() + retry_s
            while time.monotonic() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
                yield clock.make("tick")
            retry_s = min(10.0, retry_s * 2)


def replay_records(path, *, speed=0.0):
    """speed=0 is immediate; speed=1 reproduces recorded receipt spacing.

    Yields original envelopes so arrival ages do not depend on replay speed.
    A file must represent one process recording, with monotonic sequence/time.
    """
    if speed < 0 or not math.isfinite(speed):
        raise ValueError("Replay speed must be finite and nonnegative")
    previous_elapsed = None
    previous_sequence = 0
    recording_id = None
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if record["schema_version"] != 1:
                    raise ValueError("Unknown recording version")
                elapsed = record["received_elapsed_s"]
                sequence = record["sequence"]
                if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
                    raise ValueError("Invalid recording time")
                if not math.isfinite(elapsed) or elapsed < 0:
                    raise ValueError("Invalid recording time")
                if type(sequence) is not int or sequence <= previous_sequence:
                    raise ValueError("Invalid sequence")
                current_id = record["recording_id"]
                if not isinstance(current_id, str) or not current_id:
                    raise ValueError("Invalid recording id")
                if recording_id is not None and current_id != recording_id:
                    raise ValueError("Mixed recordings")
                if previous_elapsed is not None and elapsed < previous_elapsed:
                    raise ValueError("Recording time went backwards")
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"Invalid recording envelope at line {line_number}: {exc}") from exc
            if previous_elapsed is not None and speed:
                delay = (elapsed - previous_elapsed) / speed
                while delay > 0:
                    interval = min(delay, 1.0)
                    time.sleep(interval)
                    delay -= interval
            recording_id = current_id
            previous_elapsed = elapsed
            previous_sequence = sequence
            yield record
