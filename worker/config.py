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


@dataclass(frozen=True)
class ProviderInfo:
    """How to reach one LLM provider. All non-Anthropic providers speak the OpenAI
    Chat Completions protocol, so they share one adapter with a different base_url."""
    key_env: str                 # env var holding the API key
    default_model: str
    base_url: str | None = None  # None = the SDK's default endpoint
    default_rpm: int = 0         # client-side requests/minute cap (0 = none); free tiers need one
    free_tier: bool = False
    fallback_models: tuple[str, ...] = ()  # tried in order when a model's daily quota runs out


PROVIDERS: dict[str, ProviderInfo] = {
    "anthropic": ProviderInfo("ANTHROPIC_API_KEY", "claude-opus-5-5"),
    "gemini": ProviderInfo("GEMINI_API_KEY", "gemini-3.1-flash-lite",
                           base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                           default_rpm=8, free_tier=True,
                           # Free-tier quotas are per model, so another model usually still has requests left.
                           fallback_models=("gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.1-flash-lite")),
    "openai": ProviderInfo("OPENAI_API_KEY", "gpt-5.4"),
    # Any OpenAI-compatible server: Ollama (http://localhost:11434/v1), OpenRouter, Groq, vLLM, ...
    "openai_compatible": ProviderInfo("LLM_API_KEY", "", base_url=None),
    # Plays back a recorded run (LLM_CASSETTE=path); no key, no network. See worker/llm/recording.py.
    "replay": ProviderInfo("LLM_CASSETTE", ""),
}
# Auto-detection order when WORKER_PROVIDER is not set: best model first, then free.
AUTO_DETECT_ORDER = ("anthropic", "gemini", "openai")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # LLM
    provider: str = ""                     # key of PROVIDERS; empty = auto-detect from available keys
    model: str = ""                        # empty = the provider's default model
    anthropic_effort: str = "high"         # Claude: low | medium | high | xhigh | max
    reasoning_effort: str = ""             # OpenAI-protocol reasoning models: low | medium | high
    base_url: str = ""                     # overrides the provider's endpoint (needed for openai_compatible)
    rpm: int = -1                          # requests/minute cap; -1 = provider default, 0 = unlimited
    fallback_models: tuple[str, ...] | None = None  # None = the provider's defaults; () = never switch
    record: str = ""                       # LLM_RECORD: cassette file, directory, or "1" (= cassettes/)
    cassette: str = ""                     # LLM_CASSETTE: the cassette WORKER_PROVIDER=replay plays back
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
    plan_review: bool = False               # operator approves the plan before the worker acts
    use_lessons: bool = True
    # Paths
    runs_dir: Path = ROOT / "runs"
    knowledge_dir: Path = ROOT / "knowledge"
    vault_path: Path = ROOT / "config" / "vault.json"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides) -> Settings:
        s = cls(
            provider=os.environ.get("WORKER_PROVIDER", "").strip().lower(),
            model=os.environ.get("LLM_MODEL", ""),
            anthropic_effort=os.environ.get("ANTHROPIC_EFFORT", cls.anthropic_effort),
            reasoning_effort=os.environ.get("LLM_REASONING_EFFORT", ""),
            base_url=os.environ.get("LLM_BASE_URL", ""),
            rpm=int(os.environ.get("LLM_RPM", "-1")),
            max_steps=int(os.environ.get("WORKER_MAX_STEPS", cls.max_steps)),
            headless=_bool("WORKER_HEADLESS", True),
            approval_mode=os.environ.get("WORKER_APPROVAL_MODE", cls.approval_mode),
            plan_review=_bool("WORKER_PLAN_REVIEW", False),
            fallback_models=(None if os.environ.get("LLM_FALLBACK_MODELS") is None else
                             tuple(m.strip() for m in os.environ["LLM_FALLBACK_MODELS"].split(",") if m.strip())),
            use_lessons=_bool("WORKER_USE_LESSONS", True),
            start_url=os.environ.get("WORKER_START_URL", cls.start_url),
            record=os.environ.get("LLM_RECORD", ""),
            cassette=os.environ.get("LLM_CASSETTE", ""),
        )
        if os.environ.get("WORKER_TODAY"):  # pin "today" for reproducible evals
            s.extra["today"] = os.environ["WORKER_TODAY"]
        for key, value in overrides.items():
            setattr(s, key, value)
        return s

    def resolved_provider(self) -> str:
        if self.provider:
            if self.provider not in PROVIDERS:
                raise RuntimeError(f"Unknown WORKER_PROVIDER {self.provider!r}; choose from {sorted(PROVIDERS)}")
            return self.provider
        for name in AUTO_DETECT_ORDER:
            has_token = name == "anthropic" and os.environ.get("ANTHROPIC_AUTH_TOKEN")
            if os.environ.get(PROVIDERS[name].key_env) or has_token:
                return name
        raise RuntimeError("No LLM configured. Put one key in .env: ANTHROPIC_API_KEY, GEMINI_API_KEY (free tier), "
                           "or OPENAI_API_KEY - or set WORKER_PROVIDER=openai_compatible with LLM_BASE_URL/LLM_MODEL.")

    def resolved_model(self) -> str:
        name = self.resolved_provider()
        if name == "replay":
            if not self.cassette:
                raise RuntimeError("WORKER_PROVIDER=replay needs LLM_CASSETTE=path/to/cassette.json")
            return Path(self.cassette).name
        model = self.model or PROVIDERS[name].default_model
        if not model:
            raise RuntimeError(f"Set LLM_MODEL for provider {name!r}.")
        return model

    def resolved_fallbacks(self) -> list[str]:
        models = self.fallback_models
        if models is None:
            models = PROVIDERS[self.resolved_provider()].fallback_models
        primary = self.resolved_model()
        return [m for m in dict.fromkeys(models) if m != primary]

    def resolved_rpm(self) -> int:
        return PROVIDERS[self.resolved_provider()].default_rpm if self.rpm < 0 else self.rpm

    def model_label(self) -> str:
        try:
            return f"{self.resolved_provider()} / {self.resolved_model()}"
        except RuntimeError as exc:
            return f"not configured: {exc}"
