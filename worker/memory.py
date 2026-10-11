"""Memory for the worker, at three time scales.

  * Working memory  - facts discovered during this run (invoice amount, record id).
                      Written explicitly by the agent; reported with the result.
  * Company context - an operator-written onboarding doc (knowledge/company.md):
                      what systems exist, where, which credentials, house rules.
                      Swapping this file is how the same worker joins another company.
  * Lessons         - short procedural learnings the agent chooses to keep across
                      runs ("FinOps dates must be YYYY-MM-DD"). Capped and de-duplicated.
Secrets live in a separate vault and are never placed in the model's context.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

MAX_LESSONS = 25


@dataclass
class WorkingMemory:
    facts: dict[str, str] = field(default_factory=dict)

    def remember(self, key: str, value: str) -> None:
        self.facts[key.strip()] = value.strip()

    def render(self) -> str:
        if not self.facts:
            return "(empty)"
        return "\n".join(f"- {k}: {v}" for k, v in self.facts.items())


class LessonStore:
    def __init__(self, path: Path, enabled: bool = True):
        self.path = path
        self.enabled = enabled

    def load(self) -> list[str]:
        if not self.enabled or not self.path.exists():
            return []
        return [ln[2:].strip() for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]

    def add(self, lesson: str) -> bool:
        if not self.enabled:
            return False
        lesson = " ".join(lesson.split())[:300]
        lessons = self.load()
        if any(lesson.lower() == existing.lower() for existing in lessons):
            return False
        lessons = (lessons + [lesson])[-MAX_LESSONS:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("# Lessons learned by the worker\n\n" + "\n".join(f"- {l}" for l in lessons) + "\n",
                             encoding="utf-8")
        return True


class Vault:
    """Credential store. The model only ever sees credential ids, never values."""

    def __init__(self, path: Path):
        self._data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def ids(self) -> list[str]:
        return sorted(self._data)

    def get(self, credential_id: str, field_name: str) -> str:
        try:
            return self._data[credential_id][field_name]
        except KeyError as exc:
            raise KeyError(f"credential '{credential_id}' has no field '{field_name}' "
                           f"(available credentials: {self.ids()})") from exc


def load_company_context(knowledge_dir: Path) -> str:
    path = knowledge_dir / "company.md"
    return path.read_text(encoding="utf-8") if path.exists() else "(no company context provided)"
