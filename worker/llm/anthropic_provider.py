"""Claude adapter (Anthropic Messages API, manual tool loop).

Design notes:
  * The native transcript is append-only and assistant turns are appended as the
    raw `response.content` blocks, so thinking blocks are echoed back unchanged
    (required on Claude Opus 5.5 for thinking to stay valid across turns).
  * Two cache layers: an explicit breakpoint on the static system prompt and
    automatic caching of the conversation prefix, so every step pays full price
    only for the newest turn (roughly 4-5x cheaper per task than uncached).
  * Adaptive thinking with summarized display: we surface the summary in the UI
    as the agent's reasoning, which makes decisions explainable.
  * Optional server features (refusal fallbacks, server-side clearing of old tool
    results) are requested by default; if the API rejects them for this
    account/model the adapter drops them once and continues, rather than failing
    the run.
"""
from __future__ import annotations

import logging

import anthropic

from worker.llm.base import (AssistantTurn, Conversation, LLMError, LLMProvider, ToolCall,
                             ToolResult, ToolSpec, Usage)

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_EDITING_BETA = "context-management-2025-06-27"


class AnthropicConversation(Conversation):
    def __init__(self, provider: "AnthropicProvider", system: str, tools: list[ToolSpec]):
        self.p = provider
        # Stable prefix (tools + system) is cached; only the growing history varies.
        self.system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self.tools = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                      for t in tools]
        self.messages: list[dict] = []

    def add_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})

    def add_tool_results(self, results: list[ToolResult], note: str | None = None) -> None:
        # All results for one assistant turn go back in a single user message.
        content: list[dict] = [{"type": "tool_result", "tool_use_id": r.call_id, "content": r.content,
                                "is_error": r.is_error} for r in results]
        if note:
            content.append({"type": "text", "text": note})
        self.messages.append({"role": "user", "content": content})

    async def complete(self) -> AssistantTurn:
        response = await self.p.create(self.system, self.tools, self.messages)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError(f"model refused (category={getattr(details, 'category', None)})")
        self.messages.append({"role": "assistant", "content": response.content})

        text_parts, reasoning_parts, calls = [], [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking" and getattr(block, "thinking", ""):
                reasoning_parts.append(block.thinking)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
        u = response.usage
        return AssistantTurn(
            text="\n".join(text_parts).strip(),
            reasoning="\n".join(reasoning_parts).strip(),
            tool_calls=calls,
            stop_reason=response.stop_reason or "",
            usage=Usage(input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0),
            model=response.model,
        )


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str, effort: str = "high", max_tokens: int = 16000):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = anthropic.AsyncAnthropic(max_retries=4, timeout=300.0)
        self.optional_features = {"fallbacks": True, "context_editing": True}

    def conversation(self, system: str, tools: list[ToolSpec]) -> Conversation:
        return AnthropicConversation(self, system, tools)

    def _request(self, system, tools, messages) -> dict:
        kwargs: dict = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            # Automatic caching of the growing conversation: each step re-reads the history
            # from cache instead of paying full input price for every earlier page snapshot.
            cache_control={"type": "ephemeral"},
        )
        betas = []
        if self.optional_features["fallbacks"]:
            betas.append(FALLBACK_BETA)
            kwargs["fallbacks"] = "default"
        if self.optional_features["context_editing"]:
            # Server-side clearing of stale tool results (old page snapshots) once the
            # context grows large; keeps long runs cheap without editing our history.
            betas.append(CONTEXT_EDITING_BETA)
            kwargs["context_management"] = {"edits": [{"type": "clear_tool_uses_20250919"}]}
        if betas:
            kwargs["betas"] = betas
        return kwargs

    async def create(self, system, tools, messages):
        try:
            return await self.client.beta.messages.create(**self._request(system, tools, messages))
        except anthropic.BadRequestError as exc:
            if not any(self.optional_features.values()):
                raise LLMError(f"Claude rejected the request: {exc.message}", fatal=True) from exc
            log.warning("Claude rejected optional features (%s); retrying without them", exc.message)
            self.optional_features = {k: False for k in self.optional_features}
            try:
                return await self.client.beta.messages.create(**self._request(system, tools, messages))
            except anthropic.BadRequestError as exc2:
                raise LLMError(f"Claude rejected the request: {exc2.message}", fatal=True) from exc2
            except anthropic.APIError as exc2:
                raise LLMError(f"Claude API error: {exc2}") from exc2
        except anthropic.AuthenticationError as exc:
            raise LLMError("Claude authentication failed - check ANTHROPIC_API_KEY", fatal=True) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"Claude permission denied: {exc.message}", fatal=True) from exc
        except anthropic.APIError as exc:  # rate limits / 5xx already retried by the SDK
            raise LLMError(f"Claude API error: {exc}") from exc
