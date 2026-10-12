"""Independent outcome verification.

Why a separate agent rather than letting the worker grade itself:
  * Fresh context: it never saw the worker's reasoning, so it can't inherit its mistakes.
  * Fresh browser session: it observes the system of record, not the worker's last screen.
  * Read-only: policy blocks any data-changing action, so auditing can't alter the outcome.
  * Criteria come from the original request, so "did the wrong thing correctly" is caught.
If the verdict is negative, the specific issues are fed back to the worker to fix.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from worker.browser import BrowserSession
from worker.llm.base import LLMError, LLMProvider, ToolResult, ToolSpec, Usage
from worker.prompts import VERIFIER_TASK, verifier_system
from worker.tools import Tool, ToolContext, ToolExecutor, ToolOutcome, _schema, build_tools, complete_with_retries

READ_ONLY_TOOLS = {"browser_navigate", "browser_click", "browser_type", "browser_select",
                   "browser_fill_credential", "browser_back", "browser_snapshot", "read_document", "list_downloads"}


@dataclass
class Verdict:
    achieved: bool
    confidence: str = "low"
    checks: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    inconclusive: bool = False

    def to_dict(self) -> dict:
        return {"achieved": self.achieved, "confidence": self.confidence, "checks": self.checks,
                "issues": self.issues, "inconclusive": self.inconclusive}

    def feedback(self) -> str:
        failed = [c for c in self.checks if not c.get("passed")]
        lines = [f"- {c.get('check')}: {c.get('evidence', '')}" for c in failed] + [f"- {i}" for i in self.issues]
        return "\n".join(lines) or "- (no details given)"


async def report_verdict(ctx: ToolContext, args: dict) -> ToolOutcome:
    return ToolOutcome("Verdict recorded.", terminal=True, data=args)


VERDICT_TOOL = Tool(
    ToolSpec("report_verdict", "Report the audit result. Call exactly once, at the end.",
             _schema({
                 "achieved": {"type": "boolean", "description": "True only if the requested outcome is actually in place."},
                 "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                 "checks": {"type": "array", "items": _schema(
                     {"check": {"type": "string"}, "passed": {"type": "boolean"}, "evidence": {"type": "string"}},
                     ["check", "passed", "evidence"])},
                 "issues": {"type": "array", "items": {"type": "string"}},
             }, ["achieved", "confidence", "checks", "issues"])),
    report_verdict)


class Verifier:
    def __init__(self, provider: LLMProvider, company_context: str, max_steps: int, today: str | None = None):
        self.provider = provider
        self.company_context = company_context
        self.max_steps = max_steps
        self.today = today

    async def verify(self, worker_ctx: ToolContext, task: str, finish_args: dict, usage: Usage) -> Verdict:
        rec = worker_ctx.recorder
        browser: BrowserSession = await worker_ctx.browser.fork(rec.downloads_dir, rec.screenshots_dir)
        ctx = ToolContext(settings=worker_ctx.settings, browser=browser, recorder=rec, human=worker_ctx.human,
                          memory=worker_ctx.memory.__class__(), lessons=worker_ctx.lessons, vault=worker_ctx.vault,
                          read_only=True, actor="verifier", cancel=worker_ctx.cancel)
        tools = {name: t for name, t in build_tools().items() if name in READ_ONLY_TOOLS}
        tools["report_verdict"] = VERDICT_TOOL
        executor = ToolExecutor(ctx, tools)
        convo = self.provider.conversation(
            verifier_system(self.company_context, worker_ctx.vault.ids(), self.today), executor.specs())
        downloads = sorted(p.name for p in rec.downloads_dir.iterdir())
        convo.add_user_text(VERIFIER_TASK.format(
            task=task, status=finish_args.get("status"), summary=finish_args.get("summary", ""),
            claims="\n".join(f"- {c}" for c in finish_args.get("claims", [])) or "(none)",
            evidence="\n".join(f"- {e}" for e in finish_args.get("evidence", [])) or "(none)",
            mutations="\n".join(f"- {m['action']} on {m['page']}" for m in worker_ctx.mutations) or "(none)",
            downloads=", ".join(downloads) or "(none)"))
        await browser.navigate(worker_ctx.settings.start_url)
        rec.emit("verification_started", actor="verifier")
        try:
            for _ in range(self.max_steps):
                turn = await complete_with_retries(convo, worker_ctx.cancel)
                usage.add(turn.usage)
                if turn.text or turn.reasoning:
                    rec.emit("thought", actor="verifier", text=turn.text, reasoning=turn.reasoning)
                if not turn.tool_calls:
                    convo.add_user_text("Continue the audit with your tools, then call report_verdict.")
                    continue
                results, verdict_args = [], None
                for call in turn.tool_calls:
                    outcome = await executor.execute(call)
                    results.append(ToolResult(call.id, outcome.content, outcome.is_error))
                    if outcome.terminal and not outcome.is_error:
                        verdict_args = outcome.data
                if verdict_args is not None:
                    return Verdict(achieved=bool(verdict_args.get("achieved")),
                                   confidence=verdict_args.get("confidence", "low"),
                                   checks=verdict_args.get("checks", []), issues=verdict_args.get("issues", []))
                convo.add_tool_results(results)
            return Verdict(False, issues=["Verifier ran out of steps before reaching a verdict."], inconclusive=True)
        except LLMError as exc:
            return Verdict(False, issues=[f"Verifier could not run: {exc}"], inconclusive=True)
        finally:
            await browser.close()
