"""Record/replay "cassettes": watch a real model run without an API key.

RecordingProvider wraps any provider and writes every assistant turn to a JSON
cassette. ReplayProvider hands those turns back, in order, to the *real* loop,
browser and sandbox - so a recorded run is re-enacted, not just re-displayed.

Both the worker and the verifier open conversations on the same provider, so a
cassette is a list of conversations in creation order; turn N of conversation K
is replayed as turn N of the K-th conversation opened.

Replay is only faithful if the world is the same: same sandbox chaos/seed and
start state, same "today", same code (prompts, tools). We record those and warn
when what the model "sees" differs from what it saw when recorded. If the run
asks for more turns than were recorded, replay fails fast with a fatal LLMError.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

import httpx

from worker.human import HumanChannel, HumanRequest, HumanResponse
from worker.llm.base import AssistantTurn, Conversation, LLMError, LLMProvider, ToolCall, ToolResult, ToolSpec, Usage

log = logging.getLogger(__name__)
CASSETTE_VERSION = 1


def _role(system: str) -> str:
    return "verifier" if "independent auditor" in system else "worker"


class _Seen:
    """A short fingerprint of everything the model was shown since its last turn (the divergence check)."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def add(self, *parts: str) -> None:
        self.parts.extend(parts)

    def digest(self) -> str:
        value = hashlib.sha256("\x00".join(self.parts).encode()).hexdigest()[:12]
        self.parts = []
        return value


def _seen_results(seen: _Seen, results: list[ToolResult], note: str | None) -> None:
    for r in results:
        seen.add(r.call_id, r.content, str(r.is_error))
    if note:
        seen.add(note)


def _turn_from_dict(d: dict) -> AssistantTurn:
    return AssistantTurn(text=d["text"], tool_calls=[ToolCall(**c) for c in d["tool_calls"]],
                         reasoning=d["reasoning"], stop_reason=d["stop_reason"], usage=Usage(**d["usage"]),
                         model=d["model"])


def sandbox_info(start_url: str) -> dict:
    """Chaos profile, seed and a fingerprint of the sandbox's state, so replay can reset to the same world."""
    origin = "/".join(start_url.split("/")[:3])
    try:
        state = httpx.get(f"{origin}/__admin/state", timeout=3).json()
    except (httpx.HTTPError, ValueError):
        return {"url": origin, "chaos": None, "seed": None, "state_sha": None}
    data = {k: v for k, v in state.items() if k not in ("chaos", "seed", "faults_injected")}
    return {"url": origin, "chaos": state.get("chaos"), "seed": state.get("seed"),
            "state_sha": hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:12]}


# ------------------------------------------------------------------ recording

class RecordingConversation(Conversation):
    def __init__(self, inner: Conversation, entry: dict, save):
        self.inner, self.entry, self.save = inner, entry, save
        self.seen = _Seen()

    def add_user_text(self, text: str) -> None:
        self.seen.add(text)
        self.inner.add_user_text(text)

    def add_tool_results(self, results: list[ToolResult], note: str | None = None) -> None:
        _seen_results(self.seen, results, note)
        self.inner.add_tool_results(results, note)

    async def complete(self) -> AssistantTurn:
        turn = await self.inner.complete()  # on LLMError nothing is recorded; the retry sees the same input
        self.entry["turns"].append({"seen": self.seen.digest(), "turn": asdict(turn)})
        self.save()
        return turn


class RecordingProvider(LLMProvider):
    """Transparent wrapper: behaves exactly like `inner`, and writes a cassette after every turn."""

    def __init__(self, inner: LLMProvider, target: Path, start_url: str = ""):
        self.inner = inner
        self.target = target  # a .json file, or a directory that gets <run_id>.json
        self.start_url = start_url
        self.path: Path | None = None
        self.data = {"version": CASSETTE_VERSION, "provider": inner.name, "model": inner.model,
                     "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "run_id": None, "task": None,
                     "today": None, "sandbox": None, "human": [], "conversations": []}
        self.recorder = None

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model(self) -> str:
        return self.inner.model  # may change mid-run (fallback models)

    def bind_run(self, recorder, task: str, today: str | None) -> None:
        """Called by Worker.run before the first turn: the run's identity and the world it starts in."""
        self.recorder = recorder
        self.data.update(run_id=recorder.run_id, task=task, today=today or date.today().isoformat(),
                         sandbox=sandbox_info(self.start_url) if self.start_url else None)

    def conversation(self, system: str, tools: list[ToolSpec]) -> Conversation:
        entry = {"role": _role(system), "turns": []}
        self.data["conversations"].append(entry)
        return RecordingConversation(self.inner.conversation(system, tools), entry, self.save)

    def drain_notices(self) -> list[dict]:
        return self.inner.drain_notices()

    def save(self) -> None:
        if self.path is None:
            name = f"{self.data['run_id'] or time.strftime('%Y%m%d-%H%M%S')}.json"
            self.path = self.target if self.target.suffix == ".json" else self.target / name
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.recorder is not None:  # human answers, in the order they were given, so replay needs no human
            self.data["human"] = [
                {"approved": e.data.get("approved"), "answer": e.data.get("note", e.data.get("answer", ""))}
                for e in self.recorder.events if e.type in ("approval_resolved", "answer")]
        self.data["model"] = self.inner.model
        self.path.write_text(json.dumps(self.data, indent=1, default=str), encoding="utf-8")

    async def aclose(self) -> None:
        if self.data["conversations"]:
            self.save()  # final save picks up human answers given after the last turn
        await self.inner.aclose()


# ------------------------------------------------------------------ replay

class ReplayConversation(Conversation):
    def __init__(self, provider: ReplayProvider, index: int, role: str):
        self.provider, self.index = provider, index
        self.seen = _Seen()
        convos = provider.cassette["conversations"]
        self.turns = convos[index]["turns"] if index < len(convos) else None
        if self.turns is not None and convos[index]["role"] != role:
            self.turns = None  # e.g. the worker opened a verifier where the recording had none
        self.role = role
        self.pos = 0

    def add_user_text(self, text: str) -> None:
        self.seen.add(text)

    def add_tool_results(self, results: list[ToolResult], note: str | None = None) -> None:
        _seen_results(self.seen, results, note)

    async def complete(self) -> AssistantTurn:
        where = f"{self.role} conversation #{self.index + 1}, turn {self.pos + 1}"
        if self.turns is None or self.pos >= len(self.turns):
            raise LLMError(f"Replay diverged: the cassette has no {where}. The run took a different path than "
                           "the recording - usually the sandbox state, chaos/seed or code differs.", fatal=True)
        recorded = self.turns[self.pos]
        self.pos += 1
        if recorded["seen"] != self.seen.digest():
            self.provider.divergences.append(where)
            log.warning("Replay: the model's input at %s differs from the recording", where)
        return _turn_from_dict(recorded["turn"])


class ReplayProvider(LLMProvider):
    name = "replay"

    def __init__(self, cassette: dict):
        if cassette.get("version") != CASSETTE_VERSION:
            raise RuntimeError(f"Unsupported cassette version {cassette.get('version')!r}")
        self.cassette = cassette
        self.model = f"replay({cassette.get('model', '?')})"
        self.opened = 0
        self.divergences: list[str] = []  # turns where the model's input differed from the recording

    @classmethod
    def load(cls, path: str | Path) -> ReplayProvider:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def bind_run(self, recorder, task: str, today: str | None) -> None:
        if task != self.cassette.get("task"):
            log.warning("Replay: the task differs from the recorded one; the replay will likely diverge.")

    def conversation(self, system: str, tools: list[ToolSpec]) -> Conversation:
        convo = ReplayConversation(self, self.opened, _role(system))
        self.opened += 1
        return convo

    async def aclose(self) -> None:
        return None


class ReplayHumanChannel(HumanChannel):
    """Gives the recorded human answers back in order; anything beyond the recording is denied."""

    def __init__(self, answers: list[dict]):
        self.answers = list(answers)

    async def ask(self, request: HumanRequest, timeout_s: float) -> HumanResponse:
        if not self.answers:
            return HumanResponse(answer="No recorded answer.", approved=False if request.kind == "approval" else None,
                                 responder="replay")
        a = self.answers.pop(0)
        return HumanResponse(answer=a["answer"], approved=a["approved"], responder="replay")
