"""OpenAI adapter (Chat Completions with function calling).

Unlike Claude, Chat Completions history carries no signed reasoning state, so we
can manage context client-side: older tool observations (mostly page
snapshots) are elided once newer ones supersede them. The full content is
still in the run trace; the model only loses stale screens it no longer needs.
"""
from __future__ import annotations

import json

import openai

from worker.llm.base import (AssistantTurn, Conversation, LLMError, LLMProvider, ToolCall,
                             ToolResult, ToolSpec, Usage)

KEEP_RECENT_TOOL_OUTPUTS = 4
ELIDE_OVER_CHARS = 1200


class OpenAIConversation(Conversation):
    def __init__(self, provider: "OpenAIProvider", system: str, tools: list[ToolSpec]):
        self.p = provider
        self.messages: list[dict] = [{"role": "system", "content": system}]
        self.tools = [{"type": "function",
                       "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                      for t in tools]

    def add_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[ToolResult], note: str | None = None) -> None:
        for r in results:
            content = ("ERROR: " + r.content) if r.is_error else r.content
            self.messages.append({"role": "tool", "tool_call_id": r.call_id, "content": content})
        if note:
            self.messages.append({"role": "user", "content": note})

    def _compact(self) -> None:
        tool_idx = [i for i, m in enumerate(self.messages) if m["role"] == "tool"]
        for i in tool_idx[:-KEEP_RECENT_TOOL_OUTPUTS]:
            content = self.messages[i]["content"]
            if len(content) > ELIDE_OVER_CHARS and not content.startswith("[elided"):
                self.messages[i]["content"] = ("[elided older observation; first lines kept]\n"
                                               + content[:400])

    async def complete(self) -> AssistantTurn:
        self._compact()
        response = await self.p.create(self.messages, self.tools)
        choice = response.choices[0]
        msg = choice.message
        assistant: dict = {"role": "assistant", "content": msg.content or ""}
        calls: list[ToolCall] = []
        if msg.tool_calls:
            assistant["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    calls.append(ToolCall(id=tc.id, name=tc.function.name,
                                          arguments=args if isinstance(args, dict) else {}))
                except json.JSONDecodeError as exc:
                    calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments={},
                                          parse_error=f"invalid JSON arguments: {exc}"))
        self.messages.append(assistant)
        u = response.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) if u else 0
        return AssistantTurn(
            text=(msg.content or "").strip(),
            tool_calls=calls,
            stop_reason=choice.finish_reason or "",
            usage=Usage(input_tokens=u.prompt_tokens if u else 0,
                        output_tokens=u.completion_tokens if u else 0,
                        cache_read_tokens=cached or 0),
            model=response.model,
        )


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str, reasoning_effort: str = "low"):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.client = openai.AsyncOpenAI(max_retries=3, timeout=300.0)

    def conversation(self, system: str, tools: list[ToolSpec]) -> Conversation:
        return OpenAIConversation(self, system, tools)

    async def create(self, messages, tools):
        kwargs: dict = dict(model=self.model, messages=messages, tools=tools)
        if self.model.startswith(("gpt-5", "o3", "o4")):
            kwargs["reasoning_effort"] = self.reasoning_effort
        try:
            return await self.client.chat.completions.create(**kwargs)
        except openai.AuthenticationError as exc:
            raise LLMError("OpenAI authentication failed - check OPENAI_API_KEY", fatal=True) from exc
        except openai.RateLimitError as exc:
            if getattr(exc, "code", None) in ("insufficient_quota", "credit_balance_exhausted")                     or "insufficient_quota" in str(exc):
                raise LLMError("OpenAI account has no remaining credits/quota", fatal=True) from exc
            raise LLMError(f"OpenAI rate limited: {exc}") from exc
        except openai.BadRequestError as exc:
            raise LLMError(f"OpenAI rejected the request: {exc}", fatal=True) from exc
        except openai.APIError as exc:
            raise LLMError(f"OpenAI API error: {exc}") from exc
