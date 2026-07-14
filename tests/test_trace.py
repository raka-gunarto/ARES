from __future__ import annotations

import json
from pathlib import Path

from ares.core.trace import NullTracer, Tracer


def test_tracer_writes_jsonl_records(tmp_path: Path) -> None:
    """Each emit is one JSON line with ts + kind + the given fields."""
    p = tmp_path / "trace.jsonl"
    t = Tracer(str(p), enabled=True)
    t.emit("event", event_id="e1", text="hi")
    t.emit("reply", event_id="e1", content="yo", thinking="hmm")

    lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    r0, r1 = json.loads(lines[0]), json.loads(lines[1])
    assert r0["kind"] == "event" and r0["event_id"] == "e1" and r0["text"] == "hi"
    assert r1["kind"] == "reply" and r1["thinking"] == "hmm"
    assert "ts" in r0 and "ts" in r1


def test_tracer_rotates_and_retains_only_backups(tmp_path: Path) -> None:
    """Rollover caps the active file and keeps at most `backups` old generations."""
    p = tmp_path / "trace.jsonl"
    t = Tracer(str(p), max_bytes=1000, backups=3, enabled=True)
    for i in range(2000):
        t.emit("event", event_id=f"e{i}", text="x" * 40)

    trace_files = sorted(q.name for q in tmp_path.iterdir() if q.name.startswith("trace.jsonl"))
    assert "trace.jsonl" in trace_files
    assert len(trace_files) <= 4  # current + at most 3 backups
    assert "trace.jsonl.4" not in trace_files  # older generations discarded
    for n in trace_files:
        for line in (tmp_path / n).read_text().splitlines():
            if line.strip():
                json.loads(line)  # every surviving line is valid JSON


def test_tracer_truncates_huge_fields(tmp_path: Path) -> None:
    """A giant field is clipped so one record can't dominate the file."""
    p = tmp_path / "trace.jsonl"
    t = Tracer(str(p), enabled=True)
    t.emit("tool", name="x", result="A" * 50000)
    rec = json.loads(p.read_text().splitlines()[-1])
    assert len(rec["result"]) < 50000
    assert "chars]" in rec["result"]


def test_disabled_tracer_writes_nothing(tmp_path: Path) -> None:
    p = tmp_path / "trace.jsonl"
    t = Tracer(str(p), enabled=False)
    t.emit("event", event_id="e1")
    assert not p.exists()


def test_null_tracer_is_silent_noop() -> None:
    t = NullTracer()
    assert t.enabled is False
    t.emit("event", event_id="e1")  # must not raise or write
