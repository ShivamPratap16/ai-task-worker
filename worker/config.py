"""Runtime configuration, resolved from environment variables with sane defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env support (KEY=VALUE lines); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # LLM
    provider: str = ""                     # "anthropic" | "openai"; empty = auto-detect from keys
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "high"
    openai_model: str = "gpt-5.4"
    # Agent loop budgets
    max_steps: int = 60
    max_verification_rounds: int = 2
    verifier_max_steps: int = 25
    tool_retry_attempts: int = 3
    human_timeout_s: float = 600.0
    # Environment
    start_url: str = "http://127.0.0.1:8001/"
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:8001", "http://localhost:8001")
    headless: bool = True
    # Behaviour
    approval_mode: str = "risky"            # "risky" | "always" | "never"
    use_lessons: bool = True
    # Paths
    runs_dir: Path = ROOT / "runs"
    knowledge_dir: Path = ROOT / "knowledge"
    vault_path: Path = ROOT / "config" / "vault.json"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        s = cls(
            provider=os.environ.get("WORKER_PROVIDER", ""),
            anthropic_model=os.environ.get("ANTHROPIC_MODEL", cls.anthropic_model),
            anthropic_effort=os.environ.get("ANTHROPIC_EFFORT", cls.anthropic_effort),
            openai_model=os.environ.get("OPENAI_MODEL", cls.openai_model),
            max_steps=int(os.environ.get("WORKER_MAX_STEPS", cls.max_steps)),
            headless=_bool("WORKER_HEADLESS", True),
            approval_mode=os.environ.get("WORKER_APPROVAL_MODE", cls.approval_mode),
            use_lessons=_bool("WORKER_USE_LESSONS", True),
            start_url=os.environ.get("WORKER_START_URL", cls.start_url),
        )
        if os.environ.get("WORKER_TODAY"):  # pin "today" for reproducible evals
            s.extra["today"] = os.environ["WORKER_TODAY"]
        for key, value in overrides.items():
            setattr(s, key, value)
        return s

    def resolved_provider(self) -> str:
        if self.provider:
            return self.provider
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return "anthropic"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai"
        raise RuntimeError("No LLM credentials found: set ANTHROPIC_API_KEY or OPENAI_API_KEY "
                           "(or WORKER_PROVIDER explicitly).")
