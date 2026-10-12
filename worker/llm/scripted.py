"""A deterministic, scriptable provider for tests.

A "brain" is a function from the conversation so far to the next assistant
turn. Tests use it to drive the real loop, tools, browser and sandbox through
exact scenarios (denied approvals, failed verification, stalls) with no API cost.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from collections.abc import Callable

from worker.llm.base import AssistantTurn, Conversation, LLMProvider, ToolCall, ToolResult, ToolSpec

_ids = itertools.count(1)


@dataclass
class Transcript:
    system: str
    # ("user", text) | ("results", [...]) | ("note", text) | ("assistant", turn)
    entries: list[tuple[str, object]] = field(default_factory=list)

    @property
    def last_observation(self) -> str:
        for kind, payload in reversed(self.entries):
            if kind == "results" and payload:
                return payload[-1].content
            if kind == "user":
                return payload
        return ""

    def notes(self) -> list[str]:
        return [p for k, p in self.entries if k == "note"]

    def turns(self) -> int:
        return sum(1 for k, _ in self.entries if k == "assistant")

    def ref(self, kind: str, label: str) -> int:
        """Find the ref of an element in the latest snapshot, e.g. ref('button', 'Sign in')."""
        m = re.search(rf"\[(\d+)\] {kind} \"{re.escape(label)}", self.last_observation)
        if not m:
            raise AssertionError(f"{kind} {label!r} not in latest observation:\n{self.last_observation[:1500]}")
        return int(m.group(1))


Brain = Callable[[Transcript], AssistantTurn]


def call(name: str, **arguments) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids)}", name=name, arguments=arguments)


def turn(*calls: ToolCall, text: str = "") -> AssistantTurn:
    return AssistantTurn(text=text, tool_calls=list(calls))


class ScriptedConversation(Conversation):
    def __init__(self, brain: Brain, system: str):
        self.brain = brain
        self.t = Transcript(system)

    def add_user_text(self, text: str) -> None:
        self.t.entries.append(("user", text))

    def add_tool_results(self, results: list[ToolResult], note: str | None = None) -> None:
        self.t.entries.append(("results", results))
        if note:
            self.t.entries.append(("note", note))

    async def complete(self) -> AssistantTurn:
        result = self.brain(self.t)
        self.t.entries.append(("assistant", result))
        return result


class ScriptedProvider(LLMProvider):
    name = "scripted"
    model = "scripted"

    def __init__(self, worker_brain: Brain, verifier_brain: Brain | None = None):
        self.worker_brain = worker_brain
        self.verifier_brain = verifier_brain
        self.conversations: list[ScriptedConversation] = []

    def conversation(self, system: str, tools: list[ToolSpec]) -> Conversation:
        is_verifier = "independent auditor" in system
        brain = self.verifier_brain if is_verifier else self.worker_brain
        if brain is None:
            raise AssertionError("verifier was invoked but no verifier brain was scripted")
        convo = ScriptedConversation(brain, system)
        self.conversations.append(convo)
        return convo
