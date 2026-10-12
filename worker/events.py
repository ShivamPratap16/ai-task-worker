"""Run recording: an append-only event log that is the single source of truth for a run.

The UI, the CLI and the final report are all projections of this log. Events are
kept in memory (for live streaming, resumable by index) and mirrored to
runs/<id>/trace.jsonl so any run can be replayed and debugged afterwards.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Event:
    seq: int
    ts: float
    type: str          # run_started | thought | plan | action | observation | approval | question | answer | memory | verification | usage | error | note | run_finished
    data: dict[str, Any]

    def to_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "type": self.type, "data": self.data}


@dataclass
class RunRecorder:
    run_id: str
    run_dir: Path
    events: list[Event] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "screenshots").mkdir(exist_ok=True)
        (self.run_dir / "downloads").mkdir(exist_ok=True)
        self._trace = (self.run_dir / "trace.jsonl").open("a", encoding="utf-8")

    @property
    def screenshots_dir(self) -> Path:
        return self.run_dir / "screenshots"

    @property
    def downloads_dir(self) -> Path:
        return self.run_dir / "downloads"

    def emit(self, type_: str, **data: Any) -> Event:
        with self._lock:
            event = Event(seq=len(self.events), ts=time.time(), type=type_, data=data)
            self.events.append(event)
            self._trace.write(json.dumps(event.to_dict(), default=str) + "\n")
            self._trace.flush()
        return event

    def since(self, seq: int) -> list[Event]:
        with self._lock:
            return self.events[seq:]

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.run_dir / name
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    def close(self) -> None:
        self._trace.close()


@dataclass
class StoredRecorder:
    """A finished run loaded back from its trace, so history survives server restarts.

    Same read interface as RunRecorder (events, since, run_dir) but read-only.
    """
    run_id: str
    run_dir: Path
    events: list[Event]

    @classmethod
    def load(cls, run_dir: Path) -> "StoredRecorder":
        events: list[Event] = []
        for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                events.append(Event(seq=len(events), ts=d["ts"], type=d["type"], data=d["data"]))
        if events and events[-1].type != "run_finished":
            # The server stopped mid-run; close the record honestly so viewers are not left waiting.
            events.append(Event(seq=len(events), ts=events[-1].ts, type="run_finished", data={
                "outcome": "error", "summary": "Interrupted: the dashboard server stopped while this run was in progress."}))
        return cls(run_dir.name, run_dir, events)

    def since(self, seq: int) -> list[Event]:
        return self.events[seq:]
