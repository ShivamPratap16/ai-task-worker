"""The worker's control loop: plan -> act -> observe -> adapt -> verify -> report.

The LLM decides *what* to do next; this loop owns everything that must not be
left to the model's discretion:
  * budgets (steps, consecutive failures) and graceful wrap-up
  * stall detection (repeating the same failing action)
  * a hard verification gate: "completed" is only accepted after an independent
    audit, and audit findings go back to the worker to fix
  * cancellation, LLM outage handling, and a structured, honest final report
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field

from worker.browser import BrowserSession
from worker.config import Settings
from worker.events import RunRecorder
from worker.human import HumanChannel
from worker.llm import LLMError, LLMProvider, ToolResult, Usage, make_provider
from worker.memory import LessonStore, Vault, WorkingMemory, load_company_context
from worker.prompts import worker_system
from worker.tools import RunCancelled, ToolContext, ToolExecutor, build_tools, complete_with_retries
from worker.verifier import Verdict, Verifier

STALL_REPEATS = 3          # identical action this many times in a row -> intervene
MAX_CONSECUTIVE_ERRORS = 8  # failing actions in a row -> nudge to ask for help / stop
MAX_IDLE_TURNS = 2          # turns without a tool call before we stop


@dataclass
class RunResult:
    run_id: str
    task: str
    outcome: str            # completed_verified | completed_unverified | blocked | failed | error | cancelled
    summary: str
    claims: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    verification: dict | None = None
    facts: dict = field(default_factory=dict)
    steps: int = 0
    duration_s: float = 0.0
    usage: dict = field(default_factory=dict)
    approvals: dict = field(default_factory=dict)
    provider: str = ""
    model: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _signature(call) -> str:
    args = {k: v for k, v in call.arguments.items() if k != "intent"}
    return f"{call.name}:{json.dumps(args, sort_keys=True)}"


class Worker:
    def __init__(self, settings: Settings, provider: LLMProvider | None = None):
        self.settings = settings
        self.provider = provider or make_provider(settings)

    async def run(self, task: str, recorder: RunRecorder, human: HumanChannel,
                  cancel: threading.Event | None = None) -> RunResult:
        s = self.settings
        cancel = cancel or threading.Event()
        started = time.time()
        usage = Usage()
        company = load_company_context(s.knowledge_dir)
        lessons = LessonStore(s.knowledge_dir / "lessons.md", enabled=s.use_lessons)
        vault = Vault(s.vault_path)
        today = s.extra.get("today")

        browser = BrowserSession(downloads_dir=recorder.downloads_dir, screenshots_dir=recorder.screenshots_dir,
                                 headless=s.headless)
        await browser.start()
        ctx = ToolContext(settings=s, browser=browser, recorder=recorder, human=human,
                          memory=WorkingMemory(), lessons=lessons, vault=vault, cancel=cancel)
        executor = ToolExecutor(ctx, build_tools())
        if hasattr(self.provider, "bind_run"):  # record/replay cassettes need the run's identity (D20)
            self.provider.bind_run(recorder, task, today)
        verifier = Verifier(self.provider, company, s.verifier_max_steps, today)
        convo = self.provider.conversation(worker_system(company, lessons.load(), vault.ids(), today),
                                           executor.specs())
        recorder.emit("run_started", task=task, provider=self.provider.name, model=self.provider.model,
                      settings={"max_steps": s.max_steps, "approval_mode": s.approval_mode})

        result = RunResult(run_id=recorder.run_id, task=task, outcome="failed", summary="",
                           provider=self.provider.name, model=self.provider.model)
        try:
            await browser.navigate(s.start_url)
            first = await browser.snapshot()
            review = ("\n\nPLAN REVIEW IS ON: start by recording your complete plan with update_plan. Your operator "
                      "must approve it before you can take any other action." if s.plan_review else "")
            convo.add_user_text(f"TASK FROM YOUR COLLEAGUE:\n{task}{review}\n\nThe browser is open at the intranet "
                                f"home page:\n\n{first.render()}")
            await self._loop(task, convo, ctx, executor, verifier, usage, cancel, result)
        except RunCancelled:
            result.outcome, result.summary = "cancelled", "Run cancelled by the operator."
        except LLMError as exc:
            result.outcome, result.summary = "error", f"The language model was unavailable: {exc}"
            recorder.emit("error", where="llm", message=str(exc))
        except Exception as exc:  # last-resort guard: always produce a report
            result.outcome, result.summary = "error", f"Internal error: {type(exc).__name__}: {exc}"
            recorder.emit("error", where="worker", message=f"{type(exc).__name__}: {exc}")
        finally:
            await browser.close()
            await self.provider.aclose()

        # The auditor confirmed the outcome, so steps the model forgot to tick off are done.
        if result.outcome == "completed_verified" and any(s["status"] in ("pending", "in_progress") for s in ctx.plan):
            ctx.plan = [{**s, "status": "done" if s["status"] in ("pending", "in_progress") else s["status"]}
                        for s in ctx.plan]
            recorder.emit("plan", actor="worker", steps=ctx.plan)
        result.facts = dict(ctx.memory.facts)
        result.duration_s = round(time.time() - started, 1)
        result.usage = asdict(usage)
        result.approvals = {"granted": ctx.approvals_granted, "denied": ctx.approvals_denied}
        recorder.emit("run_finished", **result.to_dict())
        recorder.write_json("report.json", result.to_dict())
        (recorder.run_dir / "report.md").write_text(render_report(result), encoding="utf-8")
        return result

    async def _complete(self, convo, usage: Usage, ctx: ToolContext):
        try:
            turn = await complete_with_retries(convo, ctx.cancel)
            usage.add(turn.usage)
            return turn
        finally:
            self._report_notices(ctx)

    def _report_notices(self, ctx: ToolContext) -> None:
        for notice in self.provider.drain_notices():
            ctx.recorder.emit("model_switched", **notice)

    async def _loop(self, task, convo, ctx: ToolContext, executor: ToolExecutor, verifier: Verifier,
                    usage: Usage, cancel: threading.Event, result: RunResult) -> None:
        s, rec = self.settings, ctx.recorder
        recent: list[str] = []
        consecutive_errors = idle_turns = verification_rounds = 0

        for step in range(1, s.max_steps + 1):
            result.steps = step
            if cancel.is_set():
                result.outcome, result.summary = "cancelled", "Run cancelled by the operator."
                return
            turn = await self._complete(convo, usage, ctx)
            rec.emit("usage", input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                     cache_read_tokens=usage.cache_read_tokens)
            if turn.text or turn.reasoning:
                rec.emit("thought", actor="worker", text=turn.text, reasoning=turn.reasoning)

            if not turn.tool_calls:
                idle_turns += 1
                if idle_turns > MAX_IDLE_TURNS:
                    result.outcome = "failed"
                    result.summary = turn.text or "The worker stopped without completing or reporting."
                    return
                messages = ctx.human.drain_messages()
                convo.add_user_text("".join(f"MESSAGE FROM YOUR OPERATOR: {m}\n" for m in messages) +
                                    "You did not call a tool. Continue the task with your tools; when you are "
                                    "done or blocked, call finish.")
                continue
            idle_turns = 0

            results: list[ToolResult] = []
            finish_args = None
            for call in turn.tool_calls:
                if cancel.is_set():
                    raise RunCancelled()
                if call.name == "finish" and finish_args is None:
                    outcome = await executor.execute(call)
                    if outcome.is_error:
                        results.append(ToolResult(call.id, outcome.content, True))
                        continue
                    finish_args = outcome.data
                    finish_call_id = call.id
                    continue
                outcome = await executor.execute(call)
                results.append(ToolResult(call.id, outcome.content, outcome.is_error))
                consecutive_errors = consecutive_errors + 1 if outcome.is_error else 0
                recent.append(_signature(call) + (":err" if outcome.is_error else ""))

            notes: list[str] = []
            if finish_args is not None:
                verdict = await self._verify_if_needed(task, ctx, verifier, finish_args, usage, verification_rounds)
                self._report_notices(ctx)
                if verdict is not None:
                    verification_rounds += 1
                    rec.emit("verification", round=verification_rounds, **verdict.to_dict())
                # An inconclusive audit (the verifier's LLM failed, or it ran out of steps) says nothing about
                # the work, so it is not sent back as findings: the run ends as completed_unverified.
                if verdict is None or verdict.achieved or verdict.inconclusive \
                        or verification_rounds >= s.max_verification_rounds or finish_args.get("status") != "completed":
                    self._finalize(result, finish_args, verdict)
                    return
                results.append(ToolResult(finish_call_id, (
                    "NOT ACCEPTED: an independent audit of the live systems did not confirm the outcome.\n"
                    f"Audit findings:\n{verdict.feedback()}\n"
                    "Investigate these findings, fix what is wrong (without creating duplicates), re-check, "
                    "then call finish again."), True))

            # Stall detection: the same action (same args) repeated, typically failing the same way.
            if len(recent) >= STALL_REPEATS and len(set(recent[-STALL_REPEATS:])) == 1:
                notes.append("OPERATOR NOTE: you have repeated the identical action several times with the same "
                             "result. It is not working - change approach (re-read the page, take another route, "
                             "or ask_human if you are stuck).")
                rec.emit("stall_detected", action=recent[-1])
                recent.clear()
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                notes.append("OPERATOR NOTE: many consecutive actions have failed. Stop and reassess; if the "
                             "system is unavailable or you are blocked, finish with status 'blocked' and explain.")
                rec.emit("stall_detected", action="consecutive_errors")
                consecutive_errors = 0
            if messages := ctx.human.drain_messages():
                notes.insert(0, "MESSAGE FROM YOUR OPERATOR (it takes priority over earlier instructions where they "
                                "conflict):\n" + "\n".join(f"- {m}" for m in messages))
            remaining = s.max_steps - step
            if remaining == 5:
                notes.append("OPERATOR NOTE: 5 steps of budget left. Wrap up: verify what you have done and "
                             "call finish with an honest status.")
            convo.add_tool_results(results, note="\n".join(notes) or None)

        result.outcome = "failed"
        result.summary = ("Step budget exhausted before the task was finished. Facts gathered so far: "
                          + json.dumps(ctx.memory.facts))

    async def _verify_if_needed(self, task, ctx, verifier, finish_args, usage, rounds_done) -> Verdict | None:
        if not finish_args.get("claims") and finish_args.get("status") != "completed":
            return None  # nothing to audit (e.g. blocked before doing anything)
        if rounds_done >= self.settings.max_verification_rounds:
            return None
        return await verifier.verify(ctx, task, finish_args, usage)

    @staticmethod
    def _finalize(result: RunResult, finish_args: dict, verdict: Verdict | None) -> None:
        status = finish_args.get("status", "failed")
        if status == "completed":
            result.outcome = "completed_verified" if verdict and verdict.achieved else "completed_unverified"
        else:
            result.outcome = status
        result.summary = finish_args.get("summary", "")
        result.claims = finish_args.get("claims", [])
        result.evidence = finish_args.get("evidence", [])
        result.verification = verdict.to_dict() if verdict else None


def render_report(r: RunResult) -> str:
    lines = [f"# Run {r.run_id}", "", f"**Task:** {r.task}", "", f"**Outcome:** `{r.outcome}`", "",
             "## Summary", "", r.summary or "(none)", ""]
    if r.claims:
        lines += ["## Claims", ""] + [f"- {c}" for c in r.claims] + [""]
    if r.evidence:
        lines += ["## Evidence", ""] + [f"- {e}" for e in r.evidence] + [""]
    if r.verification:
        v = r.verification
        lines += ["## Independent verification", "",
                  f"Achieved: **{v['achieved']}** (confidence: {v['confidence']})", ""]
        lines += [f"- {'PASS' if c.get('passed') else 'FAIL'} - {c.get('check')}: {c.get('evidence', '')}"
                  for c in v.get("checks", [])]
        lines += [f"- Issue: {i}" for i in v.get("issues", [])] + [""]
    if r.facts:
        lines += ["## Facts recorded", ""] + [f"- {k}: {v}" for k, v in r.facts.items()] + [""]
    lines += ["## Run stats", "", f"- Steps: {r.steps}", f"- Duration: {r.duration_s}s",
              f"- Model: {r.provider}/{r.model}", f"- Tokens: {r.usage}", f"- Approvals: {r.approvals}"]
    return "\n".join(lines) + "\n"
