"""Adapter for every provider that speaks the OpenAI Chat Completions protocol:
OpenAI itself, Google Gemini (free tier) via its OpenAI-compatible endpoint, and
any self-hosted / third-party server (Ollama, OpenRouter, Groq, vLLM).

Provider differences handled here, not in the loop:
  * client-side request pacing for rate-limited (free) tiers, shared across runs
  * a 429 that means "daily quota gone" is fatal; a 429 that means "slow down" waits and retries
  * JSON-schema keywords some backends reject are stripped; missing tool-call ids are filled in

Chat Completions history carries no signed reasoning state, so context is
managed client-side: older tool observations (mostly page snapshots) are
elided once newer ones supersede them. The full content stays in the run trace.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections import deque

import openai

from worker.llm.base import (AssistantTurn, Conversation, LLMError, LLMProvider, ToolCall,
                             ToolResult, ToolSpec, Usage)

KEEP_RECENT_TOOL_OUTPUTS = 4
ELIDE_OVER_CHARS = 1200
RATE_LIMIT_WAITS_S = (20, 40, 60)   # waits after a per-minute 429, beyond the SDK's own short retries
DAILY_QUOTA_MARKERS = ("insufficient_quota", "credit_balance_exhausted", "perday", "per day", "per_day",
                       "requests per day", "daily")


class RateLimiter:
    """Sliding-window requests-per-minute cap, shared by every run in the process
    (runs execute on different threads and event loops, hence a threading lock)."""

    _shared: dict[str, "RateLimiter"] = {}
    _registry_lock = threading.Lock()

    def __init__(self, rpm: int):
        self.rpm = rpm
        self.stamps: deque[float] = deque()
        self.lock = threading.Lock()

    @classmethod
    def for_key(cls, key: str, rpm: int) -> "RateLimiter":
        with cls._registry_lock:
            if key not in cls._shared:
                cls._shared[key] = cls(rpm)
            return cls._shared[key]

    def _reserve(self) -> float:
        """Reserve a slot; return how long the caller must wait before using it."""
        with self.lock:
            now = time.monotonic()
            while self.stamps and now - self.stamps[0] >= 60:
                self.stamps.popleft()
            start = now if len(self.stamps) < self.rpm else self.stamps[len(self.stamps) - self.rpm] + 60
            self.stamps.append(start)
            return max(0.0, start - now)

    async def acquire(self) -> None:
        if self.rpm > 0:
            wait = self._reserve()
            if wait:
                await asyncio.sleep(wait)


def sanitize_schema(schema):
    """Drop JSON-schema keywords that some OpenAI-compatible backends (e.g. Gemini) reject."""
    if isinstance(schema, dict):
        return {k: sanitize_schema(v) for k, v in schema.items() if k not in ("additionalProperties", "strict")}
    if isinstance(schema, list):
        return [sanitize_schema(v) for v in schema]
    return schema


class OpenAIConversation(Conversation):
    def __init__(self, provider: "OpenAIProvider", system: str, tools: list[ToolSpec]):
        self.p = provider
        self.messages: list[dict] = [{"role": "system", "content": system}]
        self.tools = [{"type": "function",
                       "function": {"name": t.name, "description": t.description,
                                    "parameters": sanitize_schema(t.parameters) if provider.sanitize else t.parameters}}
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
        if not response.choices:
            raise LLMError("the model returned no choices")
        choice = response.choices[0]
        msg = choice.message
        calls: list[ToolCall] = []
        native_calls = []
        for tc in msg.tool_calls or []:
            call_id = tc.id or f"call_{uuid.uuid4().hex[:12]}"   # some compatible servers omit ids
            native_calls.append({"id": call_id, "type": "function",
                                 "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}})
            try:
                args = json.loads(tc.function.arguments or "{}")
                calls.append(ToolCall(id=call_id, name=tc.function.name,
                                      arguments=args if isinstance(args, dict) else {}))
            except json.JSONDecodeError as exc:
                calls.append(ToolCall(id=call_id, name=tc.function.name, arguments={},
                                      parse_error=f"invalid JSON arguments: {exc}"))
        assistant: dict = {"role": "assistant", "content": msg.content or None}
        if native_calls:
            assistant["tool_calls"] = native_calls
        elif not msg.content:
            assistant["content"] = ""
        self.messages.append(assistant)

        u = response.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) if u else 0
        return AssistantTurn(
            text=(msg.content or "").strip(),
            tool_calls=calls,
            stop_reason=choice.finish_reason or "",
            usage=Usage(input_tokens=(u.prompt_tokens or 0) if u else 0,
                        output_tokens=(u.completion_tokens or 0) if u else 0,
                        cache_read_tokens=cached or 0),
            model=response.model or self.p.model,
        )


class OpenAIProvider(LLMProvider):
    def __init__(self, model: str, *, name: str = "openai", api_key: str | None = None,
                 base_url: str | None = None, reasoning_effort: str = "", rpm: int = 0, sanitize: bool = False):
        self.name = name
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.sanitize = sanitize
        self.client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=2, timeout=300.0)
        self.limiter = RateLimiter.for_key(f"{base_url}|{model}", rpm)

    def conversation(self, system: str, tools: list[ToolSpec]) -> Conversation:
        return OpenAIConversation(self, system, tools)

    async def create(self, messages, tools):
        kwargs: dict = dict(model=self.model, messages=messages, tools=tools)
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        for attempt in range(len(RATE_LIMIT_WAITS_S) + 1):
            await self.limiter.acquire()
            try:
                return await self.client.chat.completions.create(**kwargs)
            except openai.AuthenticationError as exc:
                raise LLMError(f"{self.name}: authentication failed - check the API key", fatal=True) from exc
            except openai.RateLimitError as exc:
                text = str(exc).lower().replace(" ", "")
                if any(marker.replace(" ", "") in text for marker in DAILY_QUOTA_MARKERS):
                    raise LLMError(f"{self.name}: quota exhausted (daily limit or no credit). "
                                   f"Details: {str(exc)[:300]}", fatal=True) from exc
                if attempt == len(RATE_LIMIT_WAITS_S):
                    raise LLMError(f"{self.name}: still rate limited after waiting: {str(exc)[:300]}") from exc
                await asyncio.sleep(RATE_LIMIT_WAITS_S[attempt])  # per-minute limit: wait it out
            except openai.NotFoundError as exc:
                raise LLMError(f"{self.name}: model {self.model!r} not found for this key/endpoint", fatal=True) from exc
            except openai.BadRequestError as exc:
                raise LLMError(f"{self.name} rejected the request: {str(exc)[:500]}", fatal=True) from exc
            except openai.APIError as exc:
                raise LLMError(f"{self.name} API error: {str(exc)[:300]}") from exc
        raise LLMError("unreachable")
