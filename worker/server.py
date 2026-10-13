"""Control server: start tasks, watch them live, answer the worker's questions.

The event log is the source of truth; the SSE stream simply tails it from a
client-supplied cursor, so reconnecting (or opening a run later) replays the
full history with no extra state.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from worker.config import ROOT, Settings
from worker.human import HumanResponse
from worker.runner import RunManager

app = FastAPI(title="AI Task Worker")
settings = Settings.from_env()
manager = RunManager(settings)
manager.load_history()
UI = ROOT / "ui" / "dist"  # built from frontend/ (React + TypeScript) with `npm run build`
if (UI / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=UI / "assets"), name="assets")


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Only the dashboard itself may start runs or answer approvals. Browsers attach an Origin header to
    cross-site requests, so a web page in another tab cannot drive this local API. Hosts are compared without
    the scheme: behind a TLS-terminating proxy (a hosted demo) the page is https but this server sees http."""
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin \
            and urlsplit(origin).netloc != request.headers.get("host", ""):
        return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
    return await call_next(request)


# Hosted demo: with DEMO_PASSWORD set, everything sits behind a one-field sign-in, so a public URL cannot be used
# to spend the LLM quota or answer approvals. Unset (local use), the dashboard is open as before.
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "")
AUTH_COOKIE = "worker_demo_auth"
LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>AI Task Worker</title><style>body{font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0}
form{display:grid;gap:10px;width:260px}input,button{font:inherit;padding:8px}p{color:#b00;margin:0}</style></head>
<body><form method="post" action="/login"><b>AI Task Worker demo</b>{error}
<input type="password" name="password" placeholder="Demo password" autofocus>
<button>Sign in</button></form></body></html>"""


def _auth_token() -> str:
    return hashlib.sha256(f"ai-task-worker:{DEMO_PASSWORD}".encode()).hexdigest()


@app.middleware("http")
async def demo_password(request: Request, call_next):
    if not DEMO_PASSWORD or request.url.path == "/login" \
            or hmac.compare_digest(request.cookies.get(AUTH_COOKIE, ""), _auth_token()):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "sign in first"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return LOGIN_PAGE.replace("{error}", "")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    if not hmac.compare_digest(str(form.get("password", "")), DEMO_PASSWORD):
        return HTMLResponse(LOGIN_PAGE.replace("{error}", "<p>Wrong password.</p>"), status_code=401)
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response = RedirectResponse("/", status_code=303)
    # SameSite=None lets the cookie work when the demo is embedded in an iframe (Hugging Face Spaces page).
    response.set_cookie(AUTH_COOKIE, _auth_token(), httponly=True, secure=https,
                        samesite="none" if https else "lax", max_age=7 * 24 * 3600)
    return response

EXAMPLE_TASKS = [
    "Find the latest invoice from Globex, extract the amount and due date, enter it into our FinOps system, "
    "and tell me once it is done.",
    "Log every vendor invoice in the AP mailbox that isn't in FinOps yet.",
    "Initech emailed us about new bank details. Please take care of it.",
    "How much do we owe in total for invoices that are still pending in FinOps, and which one is due first?",
    "Enter the Stark Industrial invoice into FinOps.",
    "Reply to Umbrella Supplies confirming we received invoice UMB-55120 and telling them the due date we "
    "have on file.",
]


class StartRun(BaseModel):
    task: str
    approval_mode: str | None = None
    headless: bool | None = None
    plan_review: bool | None = None


class Message(BaseModel):
    text: str


class Respond(BaseModel):
    request_id: str
    answer: str = ""
    approved: bool | None = None


@app.get("/", response_class=HTMLResponse)
def index():
    page = UI / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>Dashboard not built</h1><p>Run <code>npm install && npm run build</code> in "
                            "<code>frontend/</code>, then reload.</p>", status_code=503)
    return page.read_text(encoding="utf-8")


@app.get("/api/examples")
def examples():
    return {"tasks": EXAMPLE_TASKS, "provider": _provider_label()}


def _provider_label() -> str:
    return settings.model_label()


@app.post("/api/runs")
def start_run(body: StartRun):
    if not body.task.strip():
        raise HTTPException(400, "task is empty")
    overrides = {k: v for k, v in {"approval_mode": body.approval_mode, "headless": body.headless,
                                   "plan_review": body.plan_review}.items() if v is not None}
    if os.environ.get("WORKER_FORCE_HEADLESS", "").lower() in ("1", "true", "yes"):
        overrides.pop("headless", None)  # hosted demo: no display to show a browser window on
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
    _handle(run_id)
    manager.cancel(run_id)
    return {"ok": True}


@app.post("/api/runs/{run_id}/message")
def message(run_id: str, body: Message):
    """Steer a running worker: delivered with its next observation."""
    handle = _handle(run_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "message is empty")
    if handle.status != "running":
        raise HTTPException(409, "this run has finished")
    handle.human.post_message(text)
    handle.recorder.emit("operator_message", text=text)
    return {"ok": True}


@app.get("/api/runs/{run_id}/report")
def report(run_id: str):
    path = _handle(run_id).recorder.run_dir / "report.md"
    if not path.exists():
        raise HTTPException(404, "the report is written when the run finishes")
    return FileResponse(path, media_type="text/markdown", filename=f"{run_id}-report.md")


@app.get("/api/quota")
def quota():
    """Today's request count and known daily limit for the configured model and its fallbacks."""
    try:
        models = [settings.resolved_model()] + settings.resolved_fallbacks()
    except RuntimeError:
        return {"models": []}
    from worker.quota import QuotaLedger
    ledger = QuotaLedger.shared(settings.runs_dir / "quota.json")
    rows = [{"model": m, **ledger.status(m)} for m in models]
    active = next((r["model"] for r in rows if not r["exhausted"]), None)
    return {"models": rows, "active": active}


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
