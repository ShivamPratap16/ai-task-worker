from __future__ import annotations

import os

from worker.config import PROVIDERS, Settings
from worker.llm.base import (AssistantTurn, Conversation, LLMError, LLMProvider, ToolCall, ToolResult,
                             ToolSpec, Usage)


def make_provider(settings: Settings) -> LLMProvider:
    name = settings.resolved_provider()
    model = settings.resolved_model()
    if name == "anthropic":
        from worker.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider(model, settings.anthropic_effort)

    from worker.llm.openai_provider import OpenAIProvider
    info = PROVIDERS[name]
    base_url = settings.base_url or info.base_url
    if name == "openai_compatible" and not base_url:
        raise RuntimeError("WORKER_PROVIDER=openai_compatible needs LLM_BASE_URL "
                           "(e.g. http://localhost:11434/v1 for Ollama).")
    # Local servers like Ollama ignore the key but the SDK requires one.
    api_key = os.environ.get(info.key_env) or ("not-needed" if name == "openai_compatible" else None)
    effort = settings.reasoning_effort or ("low" if name == "openai" and model.startswith(("gpt-5", "o3", "o4")) else "")
    from worker.quota import QuotaLedger
    return OpenAIProvider(model, name=name, api_key=api_key, base_url=base_url, reasoning_effort=effort,
                          rpm=settings.resolved_rpm(), sanitize=name != "openai",
                          fallback_models=settings.resolved_fallbacks(),
                          ledger=QuotaLedger.shared(settings.runs_dir / "quota.json"))


__all__ = ["AssistantTurn", "Conversation", "LLMError", "LLMProvider", "ToolCall", "ToolResult",
           "ToolSpec", "Usage", "make_provider"]
