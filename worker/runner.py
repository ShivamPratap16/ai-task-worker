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
from dataclasses import dataclass, field

from worker.agent import RunResult, Worker
from worker.config import Settings
from worker.events import RunRecorder
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

    def summary(self) -> dict:
        return {"run_id": self.run_id, "task": self.task, "status": self.status, "created": self.created,
                "outcome": self.result.outcome if self.result else None,
                "pending": [vars(r) for r in self.human.pending()]}


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

    def get(self, run_id: str) -> RunHandle | None:
        return self.runs.get(run_id)
