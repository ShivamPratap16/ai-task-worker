"""Provider layer: selection, request shapes, response parsing, rate limiting, error classification.
SDK clients are stubbed, so these run offline and cost nothing."""
import asyncio
from types import SimpleNamespace as NS

import httpx
import openai
import pytest

from worker.config import Settings
from worker.llm import LLMError, ToolResult, ToolSpec, make_provider
from worker.llm import openai_provider as oai
from worker.llm.anthropic_provider import AnthropicProvider

KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY")
TOOL = ToolSpec("ping", "Echo.", {"type": "object", "properties": {"m": {"type": "string"}},
                                  "required": ["m"], "additionalProperties": False})


@pytest.fixture
def env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------ selection

def test_auto_detect_prefers_claude_then_free_gemini(env):
    env.setenv("OPENAI_API_KEY", "x")
    env.setenv("GEMINI_API_KEY", "x")
    assert Settings().resolved_provider() == "gemini"
    env.setenv("ANTHROPIC_API_KEY", "x")
    assert Settings().resolved_provider() == "anthropic"


def test_no_keys_gives_actionable_error(env):
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        Settings().resolved_provider()


def test_gemini_uses_compatible_endpoint_and_rate_limit(env):
    env.setenv("GEMINI_API_KEY", "x")
    p = make_provider(Settings())
    assert p.name == "gemini" and p.model == "gemini-2.5-flash"
    assert "generativelanguage.googleapis.com" in str(p.client.base_url)
    assert p.limiter.rpm == 8 and p.sanitize


def test_openai_compatible_needs_base_url_and_model(env):
    with pytest.raises(RuntimeError, match="LLM_MODEL"):
        make_provider(Settings(provider="openai_compatible"))
    with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
        make_provider(Settings(provider="openai_compatible", model="qwen3"))
    p = make_provider(Settings(provider="openai_compatible", model="qwen3", base_url="http://localhost:11434/v1"))
    assert str(p.client.base_url).startswith("http://localhost:11434")


# ------------------------------------------------------------------ OpenAI-protocol adapter

def test_schema_sanitized_for_compatible_backends():
    cleaned = oai.sanitize_schema({"type": "object", "additionalProperties": False,
                                   "properties": {"a": {"type": "array", "items": {"additionalProperties": False}}}})
    assert "additionalProperties" not in str(cleaned)


def fake_completion(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls), finish_reason="tool_calls")],
              usage=NS(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None), model="m")


def make_oai(responses, **kw):
    p = oai.OpenAIProvider("m", api_key="x", **kw)
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    p.client = NS(chat=NS(completions=NS(create=create)))
    return p, calls


def test_parses_tool_calls_and_tolerates_missing_ids_and_bad_json():
    tcs = [NS(id=None, function=NS(name="ping", arguments='{"m": "ok"}')),
           NS(id="c2", function=NS(name="ping", arguments="{not json"))]
    p, calls = make_oai([fake_completion(tool_calls=tcs)], sanitize=True)
    convo = p.conversation("sys", [TOOL])
    convo.add_user_text("go")
    turn = run(convo.complete())
    assert turn.tool_calls[0].id.startswith("call_") and turn.tool_calls[0].arguments == {"m": "ok"}
    assert turn.tool_calls[1].parse_error
    assert "additionalProperties" not in str(calls[0]["tools"])
    convo.add_tool_results([ToolResult(turn.tool_calls[0].id, "pong"), ToolResult("c2", "bad", True)], note="n")
    roles = [m["role"] for m in convo.messages]
    assert roles == ["system", "user", "assistant", "tool", "tool", "user"]
    assert convo.messages[2]["tool_calls"][0]["id"] == turn.tool_calls[0].id, "ids must round-trip"


def rate_limit_error(message):
    resp = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    return openai.RateLimitError(message, response=resp, body=None)


def test_daily_quota_is_fatal(monkeypatch):
    p, _ = make_oai([rate_limit_error("Quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier")])
    with pytest.raises(LLMError) as exc:
        run(p.create([], []))
    assert exc.value.fatal


def test_per_minute_limit_waits_and_retries(monkeypatch):
    monkeypatch.setattr(oai, "RATE_LIMIT_WAITS_S", (0, 0))
    p, calls = make_oai([rate_limit_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"),
                         fake_completion(content="hi")])
    assert run(p.create([], [])).choices[0].message.content == "hi"
    assert len(calls) == 2


def test_rate_limiter_spaces_requests():
    limiter = oai.RateLimiter(rpm=2)
    assert limiter._reserve() == 0 and limiter._reserve() == 0
    assert 59 <= limiter._reserve() <= 60, "third request in a minute must wait"


# ------------------------------------------------------------------ Claude adapter

def test_claude_request_shape_and_parsing():
    p = AnthropicProvider("claude-opus-5-5", effort="high")
    seen = {}

    async def create(**kwargs):
        seen.update(kwargs)
        return NS(stop_reason="tool_use", model="claude-opus-5-5",
                  content=[NS(type="thinking", thinking="plan it"), NS(type="text", text="Opening inbox."),
                           NS(type="tool_use", id="tu1", name="ping", input={"m": "ok"})],
                  usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=80))
    p.client = NS(beta=NS(messages=NS(create=create)))
    convo = p.conversation("sys", [TOOL])
    convo.add_user_text("go")
    turn = run(convo.complete())

    assert seen["thinking"]["type"] == "adaptive" and seen["output_config"] == {"effort": "high"}
    assert seen["cache_control"] == {"type": "ephemeral"}, "conversation history must be cached"
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "tool_choice" not in seen, "forced tool choice is rejected on Opus 5.5"
    assert turn.reasoning == "plan it" and turn.text == "Opening inbox."
    assert turn.tool_calls[0].arguments == {"m": "ok"} and turn.usage.cache_read_tokens == 80
    assert convo.messages[-1]["content"][0].type == "thinking", "thinking blocks are echoed back unchanged"
