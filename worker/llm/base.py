"""Provider-neutral LLM conversation interface.

The agent loop never touches a vendor SDK. Each provider owns its *native*
transcript (so provider-specific invariants are respected - e.g. Claude's
thinking blocks must be echoed back verbatim and history must stay
append-only) and exposes the same small surface:

    convo = provider.conversation(system, tools)
    convo.add_user_text("...")
    turn = await convo.complete()
    convo.add_tool_results([...], note="optional operator note")
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema (object)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str | None = None  # set when the model emitted invalid JSON args


@dataclass
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens


@dataclass
class AssistantTurn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str = ""  # summarized thinking, when the provider exposes it
    stop_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""


class LLMError(RuntimeError):
    """Raised when the provider cannot produce a turn (after SDK-level retries).

    fatal=True means retrying cannot help (bad credentials, no credit, invalid
    request) and the run should stop immediately with a clear message.
    """

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class Conversation(ABC):
    @abstractmethod
    def add_user_text(self, text: str) -> None: ...

    @abstractmethod
    def add_tool_results(self, results: list[ToolResult], note: str | None = None) -> None: ...

    @abstractmethod
    async def complete(self) -> AssistantTurn: ...


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    def conversation(self, system: str, tools: list[ToolSpec]) -> Conversation: ...

    async def aclose(self) -> None:
        """Release network resources (HTTP connection pools)."""
        client = getattr(self, "client", None)
        if client is not None and hasattr(client, "close"):
            await client.close()
