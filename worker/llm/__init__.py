from __future__ import annotations

import os

from worker.config import Settings
from worker.llm.base import (AssistantTurn, Conversation, LLMError, LLMProvider, ToolCall,
                             ToolResult, ToolSpec, Usage)


def make_provider(settings: Settings) -> LLMProvider:
    name = settings.resolved_provider()
    if name == "anthropic":
        from worker.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider(settings.anthropic_model, settings.anthropic_effort)
    if name == "openai":
        from worker.llm.openai_provider import OpenAIProvider
        return OpenAIProvider(settings.openai_model, os.environ.get("OPENAI_REASONING_EFFORT", "low"))
    raise ValueError(f"unknown provider {name!r}")


__all__ = ["AssistantTurn", "Conversation", "LLMError", "LLMProvider", "ToolCall", "ToolResult",
           "ToolSpec", "Usage", "make_provider"]
