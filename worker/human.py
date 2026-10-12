"""Human-in-the-loop channels: how the worker asks for approval or clarification.

The agent loop only sees `HumanChannel.ask`. Where the answer comes from - the
web dashboard, a terminal prompt, or a scripted responder in evals - is a
deployment detail.
"""
from __future__ import annotations

import asyncio
import re
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class HumanRequest:
    kind: str                      # "approval" | "clarification"
    question: str
    options: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])


@dataclass
class HumanResponse:
    answer: str
    approved: bool | None = None   # set for approvals
    responder: str = "human"
    timed_out: bool = False
    cancelled: bool = False        # the run was stopped while waiting


CANCELLED = HumanResponse(answer="The run was cancelled by the operator.", approved=False, responder="operator",
                          cancelled=True)


class HumanChannel(ABC):
    @abstractmethod
    async def ask(self, request: HumanRequest, timeout_s: float) -> HumanResponse: ...

    def drain_messages(self) -> list[str]:
        """Messages the operator sent mid-run, not yet delivered to the worker."""
        return []


class WebHumanChannel(HumanChannel):
    """Requests wait (thread-safely) until the dashboard posts an answer."""

    def __init__(self) -> None:
        self._pending: dict[str, tuple[threading.Event, list[HumanResponse], HumanRequest]] = {}
        self._lock = threading.Lock()
        self._inbox: list[str] = []
        self._closed = False

    def post_message(self, text: str) -> None:
        with self._lock:
            self._inbox.append(text)

    def drain_messages(self) -> list[str]:
        with self._lock:
            messages, self._inbox = self._inbox, []
        return messages

    def close(self) -> None:
        """Run cancelled: release every pending wait now, and answer any later request immediately."""
        with self._lock:
            self._closed = True
            entries = list(self._pending.values())
        for event, box, _ in entries:
            box.append(CANCELLED)
            event.set()

    def pending(self) -> list[HumanRequest]:
        with self._lock:
            return [req for _, _, req in self._pending.values()]

    def respond(self, request_id: str, response: HumanResponse) -> bool:
        with self._lock:
            entry = self._pending.get(request_id)
        if entry is None:
            return False
        event, box, _ = entry
        box.append(response)
        event.set()
        return True

    async def ask(self, request: HumanRequest, timeout_s: float) -> HumanResponse:
        event, box = threading.Event(), []
        with self._lock:
            if self._closed:
                return CANCELLED
            self._pending[request.id] = (event, box, request)
        try:
            answered = await asyncio.to_thread(event.wait, timeout_s)
        finally:
            with self._lock:
                self._pending.pop(request.id, None)
        if not answered:
            return HumanResponse(answer="No response from a human before the timeout.", approved=False,
                                 responder="timeout", timed_out=True)
        return box[0]


class ConsoleHumanChannel(HumanChannel):
    async def ask(self, request: HumanRequest, timeout_s: float) -> HumanResponse:
        def prompt() -> HumanResponse:
            print("\n" + "=" * 70)
            print(f"WORKER NEEDS YOUR {request.kind.upper()}: {request.question}")
            for i, opt in enumerate(request.options, 1):
                print(f"  {i}. {opt}")
            if request.kind == "approval":
                raw = input("Approve? [y/N] (optionally add a note after a space): ").strip()
                approved = raw[:1].lower() == "y"
                return HumanResponse(answer=raw[1:].strip() or ("approved" if approved else "denied"),
                                     approved=approved)
            raw = input("Your answer: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(request.options):
                raw = request.options[int(raw) - 1]
            return HumanResponse(answer=raw)
        return await asyncio.to_thread(prompt)


@dataclass
class ScriptedRule:
    pattern: str                    # regex matched against the question
    answer: str = ""
    approved: bool | None = None
    kind: str | None = None


class ScriptedHumanChannel(HumanChannel):
    """Deterministic responder for evals. Unmatched approvals are denied."""

    def __init__(self, rules: list[ScriptedRule]):
        self.rules = rules
        self.log: list[tuple[HumanRequest, HumanResponse]] = []

    async def ask(self, request: HumanRequest, timeout_s: float) -> HumanResponse:
        haystack = request.question + " " + " ".join(f"{k}={v}" for k, v in request.context.items())
        for rule in self.rules:
            if (rule.kind in (None, request.kind)) and re.search(rule.pattern, haystack, re.I):
                resp = HumanResponse(answer=rule.answer or ("approved" if rule.approved else "denied"),
                                     approved=rule.approved, responder="scripted")
                break
        else:
            resp = HumanResponse(answer="No answer available; do not proceed with this.",
                                 approved=False if request.kind == "approval" else None, responder="scripted-default")
        self.log.append((request, resp))
        return resp
