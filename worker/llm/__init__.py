from __future__ import annotations

import os
from pathlib import Path

from worker.config import PROVIDERS, ROOT, Settings
from worker.llm.base import (AssistantTurn, Conversation, LLMError, LLMProvider, ToolCall, ToolResult,
                             ToolSpec, Usage)


def make_provider(settings: Settings) -> LLMProvider:
    """The configured provider; with LLM_RECORD set, wrapped so the run is saved as a cassette (D20)."""
    name = settings.resolved_provider()
    if name == "replay":
        from worker.llm.recording import ReplayProvider
        settings.resolved_model()  # raises a clear error if LLM_CASSETTE is missing
        provider = ReplayProvider.load(settings.cassette)
        # Same "today" as the recording, or date-dependent decisions (e.g. overdue) would differ.
        settings.extra = {**settings.extra, "today": provider.cassette["today"]}
        return provider
    provider = _live_provider(settings, name)
    if settings.record:
        from worker.llm.recording import RecordingProvider
        target = ROOT / "cassettes" if settings.record.lower() in {"1", "true", "yes", "on"} else Path(settings.record)
        provider = RecordingProvider(provider, target, settings.start_url)
    return provider


def _live_provider(settings: Settings, name: str) -> LLMProvider:
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
    reasoning_model = name == "openai" and model.startswith(("gpt-5", "o3", "o4"))
    effort = settings.reasoning_effort or ("low" if reasoning_model else "")
    from worker.quota import QuotaLedger
    return OpenAIProvider(model, name=name, api_key=api_key, base_url=base_url, reasoning_effort=effort,
                          rpm=settings.resolved_rpm(), sanitize=name != "openai",
                          fallback_models=settings.resolved_fallbacks(),
                          ledger=QuotaLedger.shared(settings.runs_dir / "quota.json"))


__all__ = ["AssistantTurn", "Conversation", "LLMError", "LLMProvider", "ToolCall", "ToolResult",
           "ToolSpec", "Usage", "make_provider"]
