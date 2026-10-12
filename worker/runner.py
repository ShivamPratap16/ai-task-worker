"""Run lifecycle management for the control server.

Each run executes on its own thread with its own asyncio event loop. That
isolates runs from one another and from the web server, and on Windows lets
us use a Proactor loop (required for Playwright's browser subprocess) no matter
which loop policy the ASGI server installed.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path

from worker.agent import RunResult, Worker
from worker.config import Settings
from worker.events import RunRecorder, StoredRecorder
from worker.human import WebHumanChannel


@dataclass
class RunHandle:
    run_id: str
    task: str
    recorder: RunRecorder
    human: WebHumanChannel
    cancel: threading.Event = field(default_factory=threading.Event)
    status: str = "running"
    result: RunResult | None = None
    created: float = field(default_factory=time.time)
    thread: threading.Thread | None = None

    @classmethod
    def from_disk(cls, run_dir: Path) -> "RunHandle | None":
        recorder = StoredRecorder.load(run_dir)
        if not recorder.events or recorder.events[0].type != "run_started":
            return None
        task = recorder.events[0].data.get("task", "")
        final = recorder.events[-1].data
        known = {f.name for f in fields(RunResult)} - {"run_id", "task"}
        result = RunResult(run_id=run_dir.name, task=task,
                           **{"outcome": "error", "summary": "", **{k: v for k, v in final.items() if k in known}})
        return cls(run_dir.name, task, recorder, WebHumanChannel(), status="finished", result=result,
                   created=recorder.events[0].ts)

    def summary(self) -> dict:
        r = self.result
        usage = (r.usage or {}) if r else {}
        return {"run_id": self.run_id, "task": self.task, "status": self.status, "created": self.created,
                "outcome": r.outcome if r else None,
                "pending": [vars(q) for q in self.human.pending()],
                # for the overview dashboard
                "steps": r.steps if r else None, "duration_s": r.duration_s if r else None,
                "tokens": (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)) if r else None,
                "model": r.model if r else None}


def new_event_loop() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


class RunManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.runs: dict[str, RunHandle] = {}

    def start(self, task: str, **overrides) -> RunHandle:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        settings = Settings.from_env(**{**vars(self.settings), **overrides})
        handle = RunHandle(run_id, task, RunRecorder(run_id, settings.runs_dir / run_id), WebHumanChannel())
        self.runs[run_id] = handle

        def target() -> None:
            loop = new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                worker = Worker(settings)
                handle.result = loop.run_until_complete(
                    worker.run(task, handle.recorder, handle.human, handle.cancel))
                handle.status = "finished"
            except Exception as exc:  # e.g. missing credentials before the run starts
                handle.recorder.emit("error", where="runner", message=f"{type(exc).__name__}: {exc}")
                handle.recorder.emit("run_finished", outcome="error", summary=str(exc))
                handle.status = "error"
            finally:
                handle.recorder.close()
                loop.close()

        handle.thread = threading.Thread(target=target, name=f"run-{run_id}", daemon=True)
        handle.thread.start()
        return handle

    def load_history(self) -> None:
        """Bring back finished runs from runs/*/trace.jsonl (the trace is the source of truth)."""
        if not self.settings.runs_dir.exists():
            return
        for trace in self.settings.runs_dir.glob("*/trace.jsonl"):
            if trace.parent.name in self.runs:
                continue
            try:
                handle = RunHandle.from_disk(trace.parent)
            except (OSError, ValueError, KeyError, TypeError):
                continue  # a damaged trace must not stop the dashboard
            if handle:
                self.runs[handle.run_id] = handle

    def cancel(self, run_id: str) -> None:
        handle = self.runs[run_id]
        handle.cancel.set()
        handle.human.close()  # release any approval or question the worker is waiting on

    def get(self, run_id: str) -> RunHandle | None:
        return self.runs.get(run_id)
