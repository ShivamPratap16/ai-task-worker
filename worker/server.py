"""Control server: start tasks, watch them live, answer the worker's questions.

The event log is the source of truth; the SSE stream simply tails it from a
client-supplied cursor, so reconnecting (or opening a run later) replays the
full history with no extra state.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from worker.config import ROOT, Settings
from worker.human import HumanResponse
from worker.runner import RunManager

app = FastAPI(title="AI Task Worker")
settings = Settings.from_env()
manager = RunManager(settings)
UI = ROOT / "ui" / "index.html"

EXAMPLE_TASKS = [
    "Find the latest invoice from Globex, extract the amount and due date, enter it into our FinOps system, and tell me once it is done.",
    "Log every vendor invoice in the AP mailbox that isn't in FinOps yet.",
    "Initech emailed us about new bank details. Please take care of it.",
    "How much do we owe in total for invoices that are still pending in FinOps, and which one is due first?",
    "Enter the Stark Industrial invoice into FinOps.",
    "Reply to Umbrella Supplies confirming we received invoice UMB-55120 and telling them the due date we have on file.",
]


class StartRun(BaseModel):
    task: str
    approval_mode: str | None = None
    headless: bool | None = None


class Respond(BaseModel):
    request_id: str
    answer: str = ""
    approved: bool | None = None


@app.get("/", response_class=HTMLResponse)
def index():
    return UI.read_text(encoding="utf-8")


@app.get("/api/examples")
def examples():
    return {"tasks": EXAMPLE_TASKS, "provider": _provider_label()}


def _provider_label() -> str:
    try:
        name = settings.resolved_provider()
    except RuntimeError as exc:
        return f"not configured: {exc}"
    return f"{name} / {settings.anthropic_model if name == 'anthropic' else settings.openai_model}"


@app.post("/api/runs")
def start_run(body: StartRun):
    if not body.task.strip():
        raise HTTPException(400, "task is empty")
    overrides = {k: v for k, v in {"approval_mode": body.approval_mode, "headless": body.headless}.items()
                 if v is not None}
    handle = manager.start(body.task.strip(), **overrides)
    return handle.summary()


@app.get("/api/runs")
def list_runs():
    return [h.summary() for h in sorted(manager.runs.values(), key=lambda h: h.created, reverse=True)]


def _handle(run_id: str):
    handle = manager.get(run_id)
    if handle is None:
        raise HTTPException(404, "unknown run")
    return handle


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    handle = _handle(run_id)
    return {**handle.summary(), "result": handle.result.to_dict() if handle.result else None}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request, after: int = 0):
    handle = _handle(run_id)

    async def gen():
        cursor = after
        while True:
            if await request.is_disconnected():
                return
            for event in handle.recorder.since(cursor):
                cursor = event.seq + 1
                yield f"id: {event.seq}\ndata: {json.dumps(event.to_dict(), default=str)}\n\n"
            if handle.status != "running" and cursor >= len(handle.recorder.events):
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs/{run_id}/respond")
def respond(run_id: str, body: Respond):
    handle = _handle(run_id)
    ok = handle.human.respond(body.request_id, HumanResponse(answer=body.answer or (
        "approved" if body.approved else "denied" if body.approved is False else ""), approved=body.approved,
        responder="operator"))
    if not ok:
        raise HTTPException(409, "that request is no longer pending")
    return {"ok": True}


@app.post("/api/runs/{run_id}/cancel")
def cancel(run_id: str):
    _handle(run_id).cancel.set()
    return {"ok": True}


@app.get("/api/runs/{run_id}/files/{kind}/{name}")
def run_file(run_id: str, kind: str, name: str):
    handle = _handle(run_id)
    if kind not in ("screenshots", "downloads"):
        raise HTTPException(404)
    root = (handle.recorder.run_dir / kind).resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)
