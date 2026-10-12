"""Tools the worker can use, and the executor that runs them safely.

Every tool call flows through `ToolExecutor.execute`, which provides the
reliability and safety guarantees in one place:

  1. argument validation          -> precise error the model can fix
  2. policy (URL allowlist, risky-action approval, read-only mode)
  3. transient-failure retries    -> exponential backoff, invisible to the model
  4. error classification         -> failures come back as observations, never crashes
  5. evidence capture             -> screenshot + trace event after every browser action

Tools are plain async functions over a ToolContext, so adding a capability
(an API client, a spreadsheet, a desktop app) means adding a function here; the
agent loop does not change.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Awaitable, Callable

from pypdf import PdfReader

from worker import policy
from worker.browser import BrowserActionError, BrowserSession
from worker.config import Settings
from worker.events import RunRecorder
from worker.human import HumanChannel, HumanRequest
from worker.llm.base import ToolCall, ToolSpec
from worker.memory import LessonStore, Vault, WorkingMemory


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    error_kind: str = ""
    terminal: bool = False          # finish: ends the agent loop
    data: dict = field(default_factory=dict)


class PolicyBlocked(Exception):
    pass


class RunCancelled(Exception):
    """The operator stopped the run. Propagates straight out of the loop (never becomes an observation)."""


async def until_cancelled(awaitable, cancel: threading.Event):
    """Await something slow (an LLM call, a rate-limit wait) but give up promptly if the run is cancelled."""
    task = asyncio.ensure_future(awaitable)
    while True:
        done, _ = await asyncio.wait({task}, timeout=0.5)
        if done:
            return task.result()
        if cancel.is_set():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise RunCancelled()


# Waits between loop-level LLM retries. Long enough to ride out a short network drop or a provider's
# "high demand" spell (about 2 minutes in total); the SDK's own quick retries come first.
LLM_RETRY_WAITS_S = (5, 15, 30, 60)


async def complete_with_retries(convo, cancel: threading.Event, on_retry=None):
    """One LLM turn, with loop-level retries on top of the SDK's own (connection drops, a provider's 503
    "high demand"). Fatal errors (no credit, bad key) are raised at once. Cancellable throughout."""
    from worker.llm.base import LLMError
    for attempt in range(len(LLM_RETRY_WAITS_S) + 1):
        try:
            return await until_cancelled(convo.complete(), cancel)
        except LLMError as exc:
            if exc.fatal or attempt == len(LLM_RETRY_WAITS_S):
                raise
            wait = LLM_RETRY_WAITS_S[attempt]
            if on_retry:
                on_retry(attempt + 1, wait, str(exc))
            await until_cancelled(asyncio.sleep(wait), cancel)


def llm_retry_reporter(recorder, actor: str):
    """on_retry callback that puts each LLM retry in the trace, so a slow step is explained."""
    def report(attempt: int, wait_s: float, error: str) -> None:
        recorder.emit("llm_retry", actor=actor, attempt=attempt, wait_s=wait_s, error=error[:300])
    return report


async def _evidence(ctx: ToolContext, label: str) -> str | None:
    """Screenshot for the human, best effort: evidence capture must never break the run."""
    try:
        return (await ctx.browser.screenshot(label)).name if ctx.browser.page is not None else None
    except Exception:
        return None


@dataclass
class ToolContext:
    settings: Settings
    browser: BrowserSession
    recorder: RunRecorder
    human: HumanChannel
    memory: WorkingMemory
    lessons: LessonStore
    vault: Vault
    read_only: bool = False         # verifier mode
    actor: str = "worker"           # "worker" | "verifier" - tags trace events
    plan: list[dict] = field(default_factory=list)
    approvals_granted: int = 0
    approvals_denied: int = 0
    mutations: list[dict] = field(default_factory=list)  # data-changing actions, for the verifier
    cancel: threading.Event = field(default_factory=threading.Event)
    plan_approved: bool = False     # plan review mode: set once the operator approves the plan

    async def request_approval(self, action: str, reason: str) -> None:
        """Block until a human approves; raise PolicyBlocked on denial."""
        if self.read_only:
            raise PolicyBlocked(f"read-only mode: {reason}")
        req = HumanRequest(kind="approval", question=f"The worker wants to: {action}",
                           context={"reason": reason, "page": self.browser.page.url})
        self.recorder.emit("approval_requested", actor=self.actor, request_id=req.id, action=action, reason=reason,
                           screenshot=await _evidence(self, f"{self.actor}-approval"), url=self.browser.page.url)
        resp = await self.human.ask(req, self.settings.human_timeout_s)
        if resp.cancelled:
            raise RunCancelled()
        self.recorder.emit("approval_resolved", actor=self.actor, request_id=req.id, approved=bool(resp.approved),
                           note=resp.answer, responder=resp.responder)
        if not resp.approved:
            self.approvals_denied += 1
            raise PolicyBlocked(f"A human DENIED this action ({resp.answer}). Do not retry it or work around it; "
                                "continue with the rest of the task if possible and report this in your summary.")
        self.approvals_granted += 1


ToolFn = Callable[[ToolContext, dict], Awaitable[ToolOutcome]]


@dataclass
class Tool:
    spec: ToolSpec
    fn: ToolFn
    browser_action: bool = False    # returns a page observation; gets screenshot evidence
    available_read_only: bool = True


INTENT = {"type": "string",
          "description": "One short sentence: why you are taking this action (shown to the human operator)."}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# ------------------------------------------------------------------ browser tools

async def _observe(ctx: ToolContext, header: str) -> ToolOutcome:
    snap = await ctx.browser.snapshot()
    parts = [header]
    downloads = ctx.browser.take_new_downloads()
    if downloads:
        names = ", ".join(p.name for p in downloads)
        parts.append(f"Downloaded file(s): {names} (use read_document to read them).")
    if snap.http_status and snap.http_status >= 500:
        parts.append(f"WARNING: the server answered HTTP {snap.http_status}; the action likely did not take effect.")
    parts.append("\n" + snap.render())
    return ToolOutcome("\n".join(parts), data={"url": snap.url, "http_status": snap.http_status})


async def _guard_url(ctx: ToolContext, url: str) -> str:
    base = ctx.browser.page.url if ctx.browser.page.url.startswith("http") else ctx.settings.start_url
    decision = policy.check_url(url, ctx.settings.allowed_origins, base=base)
    if not decision.allowed:
        raise PolicyBlocked(decision.reason)
    from urllib.parse import urljoin
    return urljoin(base, url)


async def _guard_interaction(ctx: ToolContext, ref, *, submits: bool, verb: str) -> dict:
    info = await ctx.browser.describe(ref)
    if info["tag"] == "a" and info["href"]:
        await _guard_url(ctx, info["href"])
    is_submit = submits and (info["tag"] == "button" or info["type"] in ("submit", "image"))
    if info.get("form_method") and not info.get("form_action"):
        info["form_action"] = ctx.browser.page.url  # a form without action= posts to the current page
    decision = policy.assess_interaction(info, submits=is_submit or (verb == "enter"),
                                         mode=ctx.settings.approval_mode, read_only=ctx.read_only)
    if not decision.allowed:
        raise PolicyBlocked(decision.reason)
    label = info.get("form_submit_label") if verb == "enter" else info.get("text")
    if decision.needs_approval:
        how = "press" if verb != "enter" else "submit form via"
        await ctx.request_approval(f"{how} '{label}' on {ctx.browser.page.url}",
                                   decision.reason)
    changes_data = policy.risky_endpoint(info) or (
        info.get("form_method", "post") != "get" and not policy.SAFE_LABEL.match(label or ""))
    if (is_submit or verb == "enter") and changes_data:
        ctx.mutations.append({"action": label, "page": ctx.browser.page.url, "at": time.time()})
    return info


async def browser_navigate(ctx: ToolContext, args: dict) -> ToolOutcome:
    url = await _guard_url(ctx, args["url"])
    await ctx.browser.navigate(url)
    return await _observe(ctx, f"Navigated to {url}.")


async def browser_click(ctx: ToolContext, args: dict) -> ToolOutcome:
    info = await _guard_interaction(ctx, args["ref"], submits=True, verb="click")
    await ctx.browser.click(args["ref"])
    return await _observe(ctx, f"Clicked [{args['ref']}] {info['tag']} '{info['text'][:60]}'.")


async def browser_type(ctx: ToolContext, args: dict) -> ToolOutcome:
    press_enter = bool(args.get("press_enter", False))
    if press_enter:
        await _guard_interaction(ctx, args["ref"], submits=False, verb="enter")
    await ctx.browser.type(args["ref"], args["text"], press_enter=press_enter)
    return await _observe(ctx, f"Typed into [{args['ref']}]{' and pressed Enter' if press_enter else ''}.")


async def browser_select(ctx: ToolContext, args: dict) -> ToolOutcome:
    chosen = await ctx.browser.select(args["ref"], args["option"])
    return await _observe(ctx, f"Selected '{chosen}' in [{args['ref']}].")


async def browser_fill_credential(ctx: ToolContext, args: dict) -> ToolOutcome:
    try:
        secret = ctx.vault.get(args["credential_id"], args["field"])
    except KeyError as exc:
        return ToolOutcome(str(exc), is_error=True, error_kind="bad_credential")
    await ctx.browser.type(args["ref"], secret)
    return await _observe(ctx, f"Filled [{args['ref']}] with {args['credential_id']}.{args['field']} from the vault.")


async def browser_back(ctx: ToolContext, args: dict) -> ToolOutcome:
    await ctx.browser.back()
    return await _observe(ctx, "Went back.")


async def browser_snapshot(ctx: ToolContext, args: dict) -> ToolOutcome:
    wait = min(float(args.get("wait_seconds", 0) or 0), 10.0)
    if wait:
        await asyncio.sleep(wait)
    return await _observe(ctx, "Current page:")


# ------------------------------------------------------------------ file tools

def _resolve_download(ctx: ToolContext, filename: str) -> Path:
    root = ctx.recorder.downloads_dir.resolve()
    path = (root / filename).resolve()
    if path.parent != root:
        raise PolicyBlocked("only files in the run's downloads folder can be read")
    return path


async def read_document(ctx: ToolContext, args: dict) -> ToolOutcome:
    path = _resolve_download(ctx, args["filename"])
    if not path.exists():
        available = [p.name for p in ctx.recorder.downloads_dir.iterdir()]
        return ToolOutcome(f"No downloaded file named {args['filename']!r}. Available: {available}",
                           is_error=True, error_kind="not_found")
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        if not text.strip():
            return ToolOutcome("The PDF contains no extractable text (it may be a scanned image).",
                               is_error=True, error_kind="unreadable")
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    return ToolOutcome(f"Contents of {path.name}:\n{text[:12000]}")


async def list_downloads(ctx: ToolContext, args: dict) -> ToolOutcome:
    files = sorted(p.name for p in ctx.recorder.downloads_dir.iterdir())
    return ToolOutcome("Downloaded files: " + (", ".join(files) if files else "(none)"))


# ------------------------------------------------------------------ cognition tools

async def update_plan(ctx: ToolContext, args: dict) -> ToolOutcome:
    ctx.plan = [{"step": s.get("step", ""), "status": s.get("status", "pending")} for s in args["steps"]]
    ctx.recorder.emit("plan", actor=ctx.actor, steps=ctx.plan)
    if not ctx.settings.plan_review or ctx.plan_approved:
        return ToolOutcome("Plan recorded.")
    # Plan review mode: the operator approves (or redirects) the plan before the worker may act.
    req = HumanRequest(kind="plan_review", question="Review the worker's plan before it starts",
                       context={"page": ctx.browser.page.url})
    ctx.recorder.emit("question", actor=ctx.actor, request_id=req.id, kind="plan_review", question=req.question,
                      options=[], steps=ctx.plan)
    resp = await ctx.human.ask(req, ctx.settings.human_timeout_s)
    ctx.recorder.emit("answer", actor=ctx.actor, request_id=req.id, answer=resp.answer, approved=resp.approved,
                      responder=resp.responder)
    if resp.cancelled:
        raise RunCancelled()
    if resp.timed_out:
        return ToolOutcome("Nobody approved the plan in time. Do not act; finish with status 'blocked'.",
                           is_error=True, error_kind="human_timeout")
    if resp.approved:
        ctx.plan_approved = True
        note = resp.answer if resp.answer not in ("", "approved") else ""
        return ToolOutcome("Plan recorded. The operator APPROVED it; go ahead."
                           + (f" Operator note: {note}" if note else ""))
    return ToolOutcome(f"The operator did NOT approve this plan and asked for changes: {resp.answer}\n"
                       "Revise the plan accordingly and call update_plan again.")


async def remember(ctx: ToolContext, args: dict) -> ToolOutcome:
    ctx.memory.remember(args["key"], args["value"])
    ctx.recorder.emit("memory", actor=ctx.actor, key=args["key"], value=args["value"])
    return ToolOutcome("Saved. Working memory now:\n" + ctx.memory.render())


async def save_lesson(ctx: ToolContext, args: dict) -> ToolOutcome:
    added = ctx.lessons.add(args["lesson"])
    if added:
        ctx.recorder.emit("lesson", actor=ctx.actor, lesson=args["lesson"])
    return ToolOutcome("Lesson saved for future runs." if added else "Lesson already known (or lessons disabled).")


async def ask_human(ctx: ToolContext, args: dict) -> ToolOutcome:
    kind = args.get("kind", "clarification")
    req = HumanRequest(kind=kind, question=args["question"], options=args.get("options") or [],
                       context={"page": ctx.browser.page.url})
    ctx.recorder.emit("question", actor=ctx.actor, request_id=req.id, kind=kind, question=req.question,
                      options=req.options,
                      screenshot=await _evidence(ctx, f"{ctx.actor}-question"))
    resp = await ctx.human.ask(req, ctx.settings.human_timeout_s)
    ctx.recorder.emit("answer", actor=ctx.actor, request_id=req.id, answer=resp.answer, approved=resp.approved,
                      responder=resp.responder)
    if resp.cancelled:
        raise RunCancelled()
    if resp.timed_out:
        return ToolOutcome("No human answered in time. Do not proceed with the uncertain part; finish with "
                           "status 'blocked' explaining what you need.", is_error=True, error_kind="human_timeout")
    verdict = "" if resp.approved is None else (" (APPROVED)" if resp.approved else " (DENIED)")
    return ToolOutcome(f"Human answered{verdict}: {resp.answer}")


async def finish(ctx: ToolContext, args: dict) -> ToolOutcome:
    return ToolOutcome("Finish requested.", terminal=True, data=args)


# ------------------------------------------------------------------ registry

def build_tools() -> dict[str, Tool]:
    tools = [
        Tool(ToolSpec("browser_navigate", "Open a URL in the browser. Returns the new page snapshot.",
                      _schema({"url": {"type": "string"}, "intent": INTENT}, ["url", "intent"])),
             browser_navigate, browser_action=True),
        Tool(ToolSpec("browser_click", "Click the element with the given ref from the latest snapshot "
                      "(links, buttons, checkboxes). Returns the resulting page snapshot.",
                      _schema({"ref": {"type": "integer"}, "intent": INTENT}, ["ref", "intent"])),
             browser_click, browser_action=True),
        Tool(ToolSpec("browser_type", "Replace the contents of a text field with `text`. "
                      "Set press_enter to submit the field's form.",
                      _schema({"ref": {"type": "integer"}, "text": {"type": "string"},
                               "press_enter": {"type": "boolean"}, "intent": INTENT}, ["ref", "text", "intent"])),
             browser_type, browser_action=True),
        Tool(ToolSpec("browser_select", "Choose an option in a dropdown (combobox) by its visible text.",
                      _schema({"ref": {"type": "integer"}, "option": {"type": "string"}, "intent": INTENT},
                              ["ref", "option", "intent"])),
             browser_select, browser_action=True),
        Tool(ToolSpec("browser_fill_credential", "Type a secret from the credential vault into a field without "
                      "revealing it. Use for usernames/passwords listed in the company context.",
                      _schema({"ref": {"type": "integer"}, "credential_id": {"type": "string"},
                               "field": {"type": "string", "enum": ["username", "password"]}, "intent": INTENT},
                              ["ref", "credential_id", "field", "intent"])),
             browser_fill_credential, browser_action=True),
        Tool(ToolSpec("browser_back", "Go back to the previous page.", _schema({"intent": INTENT}, ["intent"])),
             browser_back, browser_action=True),
        Tool(ToolSpec("browser_snapshot", "Re-read the current page, optionally after waiting a few seconds "
                      "(for slow pages).", _schema({"wait_seconds": {"type": "number"}}, [])),
             browser_snapshot, browser_action=True),
        Tool(ToolSpec("read_document", "Read the text of a file you downloaded (PDF, txt, csv, json).",
                      _schema({"filename": {"type": "string"}}, ["filename"])), read_document),
        Tool(ToolSpec("list_downloads", "List files downloaded during this task.", _schema({}, [])), list_downloads),
        Tool(ToolSpec("update_plan", "Record or revise your step-by-step plan. Call it at the start and whenever "
                      "the plan changes or a step completes.",
                      _schema({"steps": {"type": "array", "items": _schema(
                          {"step": {"type": "string"},
                           "status": {"type": "string",
                                      "enum": ["pending", "in_progress", "done", "skipped", "failed"]}},
                          ["step", "status"])}}, ["steps"])),
             update_plan, available_read_only=False),
        Tool(ToolSpec("remember", "Store a fact you discovered (e.g. an amount, a date, a record id) in working "
                      "memory so you can rely on it later and report it.",
                      _schema({"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"])),
             remember),
        Tool(ToolSpec("save_lesson", "Save a short, reusable, non-sensitive lesson about how these systems work, for "
                      "future tasks (e.g. a required input format). Do not save task-specific data.",
                      _schema({"lesson": {"type": "string"}}, ["lesson"])),
             save_lesson, available_read_only=False),
        Tool(ToolSpec("ask_human", "Ask your human supervisor. Use kind='clarification' when the request is "
                      "ambiguous and guessing could cause a wrong outcome; kind='approval' before any sensitive "
                      "action that is not clearly within the request. Blocks until answered.",
                      _schema({"kind": {"type": "string", "enum": ["clarification", "approval"]},
                               "question": {"type": "string"},
                               "options": {"type": "array", "items": {"type": "string"}}},
                              ["kind", "question"])),
             ask_human, available_read_only=False),
        Tool(ToolSpec("finish", "End the task. Call only after you have checked the outcome in the system itself. "
                      "status: completed = the goal is achieved; blocked = you cannot proceed without something "
                      "from a human; failed = it cannot be done.",
                      _schema({"status": {"type": "string", "enum": ["completed", "blocked", "failed"]},
                               "summary": {"type": "string", "description": "Concise report for the requester."},
                               "claims": {"type": "array", "items": {"type": "string"},
                                          "description": "Specific, checkable statements about the end state, "
                                                         "e.g. 'FinOps invoice GLX-2026-0934 exists with amount "
                                                         "16245.50 USD'."},
                               "evidence": {"type": "array", "items": {"type": "string"},
                                            "description": "Where each claim can be observed (URLs, record ids, "
                                                           "file names)."}},
                              ["status", "summary", "claims"])),
             finish, available_read_only=False),
    ]
    return {t.spec.name: t for t in tools}


PLAN_REVIEW_ALLOWED = {"update_plan", "remember", "ask_human", "finish"}


def _validate(spec: ToolSpec, args: dict) -> str | None:
    props, required = spec.parameters.get("properties", {}), spec.parameters.get("required", [])
    missing = [r for r in required if r not in args]
    if missing:
        return f"missing required argument(s): {missing}"
    unknown = [k for k in args if k not in props]
    if unknown:
        return f"unknown argument(s): {unknown}; expected {list(props)}"
    type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
    for key, value in args.items():
        expected = props[key].get("type")
        if expected == "integer" and isinstance(value, str) and value.strip().isdigit():
            args[key] = int(value)  # tolerate "12" for refs
            continue
        if expected in type_map and not isinstance(value, type_map[expected]):
            return f"argument {key!r} must be of type {expected}"
        if "enum" in props[key] and value not in props[key]["enum"]:
            return f"argument {key!r} must be one of {props[key]['enum']}"
    return None


class ToolExecutor:
    def __init__(self, ctx: ToolContext, tools: dict[str, Tool]):
        self.ctx = ctx
        self.tools = tools

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.tools.values()]

    async def execute(self, call: ToolCall) -> ToolOutcome:
        ctx = self.ctx
        tool = self.tools.get(call.name)
        ctx.recorder.emit("action", actor=ctx.actor, call_id=call.id, tool=call.name,
                          args=call.arguments, intent=call.arguments.get("intent", ""))
        if tool is None:
            outcome = ToolOutcome(f"Unknown tool {call.name!r}. Available: {sorted(self.tools)}", True, "unknown_tool")
        elif call.parse_error:
            outcome = ToolOutcome(f"Your arguments could not be parsed ({call.parse_error}). Re-issue the call.",
                                  True, "bad_arguments")
        elif (problem := _validate(tool.spec, call.arguments)) is not None:
            outcome = ToolOutcome(f"Invalid arguments for {call.name}: {problem}", True, "bad_arguments")
        elif ctx.settings.plan_review and not ctx.read_only and not ctx.plan_approved \
                and call.name not in PLAN_REVIEW_ALLOWED:
            outcome = ToolOutcome("Plan review is on: first record your complete plan with update_plan. The operator "
                                  "must approve it before you take any action.", True, "plan_not_approved")
        else:
            outcome = await self._run_with_retries(tool, call)

        shot = None
        if tool is not None and tool.browser_action and ctx.browser.page is not None:
            try:
                shot = await ctx.browser.screenshot(f"{ctx.actor}-{call.name}")
            except Exception:  # evidence capture must never break the run
                shot = None
        ctx.recorder.emit("observation", actor=ctx.actor, call_id=call.id, tool=call.name, ok=not outcome.is_error,
                          error_kind=outcome.error_kind, content=outcome.content[:4000],
                          screenshot=shot.name if shot else None, url=outcome.data.get("url"))
        return outcome

    async def _run_with_retries(self, tool: Tool, call: ToolCall) -> ToolOutcome:
        attempts = self.ctx.settings.tool_retry_attempts
        for attempt in range(1, attempts + 1):
            try:
                return await tool.fn(self.ctx, call.arguments)
            except RunCancelled:
                raise
            except PolicyBlocked as exc:
                self.ctx.recorder.emit("policy_block", actor=self.ctx.actor, tool=call.name, reason=str(exc))
                return ToolOutcome(f"BLOCKED by policy: {exc}", True, "policy_blocked")
            except BrowserActionError as exc:
                if exc.retryable and attempt < attempts:
                    delay = 0.5 * 2 ** (attempt - 1)
                    self.ctx.recorder.emit("retry", actor=self.ctx.actor, tool=call.name, attempt=attempt,
                                           error=str(exc), delay_s=delay)
                    await asyncio.sleep(delay)
                    continue
                return await self._error_with_page(f"{exc}", exc.kind)
            except Exception as exc:  # an unexpected tool failure is an observation, not a crash
                first_line = str(exc).splitlines()[0][:300] if str(exc) else ""
                return await self._error_with_page(f"Tool error ({type(exc).__name__}): {first_line}",
                                                   "tool_exception")
        return ToolOutcome("unreachable", True)

    async def _error_with_page(self, message: str, kind: str) -> ToolOutcome:
        """Failed actions still return what the page looks like now, so the model can re-orient."""
        try:
            snap = await self.ctx.browser.snapshot()
            return ToolOutcome(f"ACTION FAILED: {message}\n\nCurrent page:\n{snap.render()}", True, kind,
                               data={"url": snap.url})
        except Exception:
            return ToolOutcome(f"ACTION FAILED: {message}", True, kind)
