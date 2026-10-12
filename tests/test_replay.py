"""Record/replay cassettes: a recorded run re-enacts against the real loop, browser and sandbox."""
import asyncio
import json

import pytest

from worker.human import ScriptedHumanChannel, ScriptedRule
from worker.llm import LLMError, make_provider
from worker.llm.recording import RecordingProvider, ReplayHumanChannel, ReplayProvider
from worker.llm.scripted import ScriptedProvider, Transcript, call, turn

from tests.test_agent_loop import INTENT, login_steps


def _pay_invoice_2(sandbox_url):
    """Worker signs in, pays invoice 2 (needs approval), claims success; verifier checks, then agrees."""
    def worker(t: Transcript):
        step = t.turns()
        if (s := login_steps(t, step, sandbox_url)) is not None:
            return s
        if step == 4:
            return turn(call("browser_click", ref=t.ref("button", "Submit for payment"), intent="pay it"))
        return turn(call("finish", status="completed", summary="Invoice 2 submitted for payment.",
                         claims=["Invoice 2 is Submitted for payment"]))

    def verifier(t: Transcript):
        if t.turns() == 0:
            return turn(call("browser_navigate", url="/mail", intent=INTENT))
        return turn(call("report_verdict", achieved=True, confidence="high", issues=[],
                         checks=[{"check": "status", "passed": True, "evidence": "seen"}]))
    return ScriptedProvider(worker, verifier)


def _invoice(sandbox_state, id_):
    return next(i for i in sandbox_state()["invoices"] if i["id"] == id_)


@pytest.fixture
def cassette(run_worker, reset_sandbox, sandbox_state, sandbox_url, tmp_path, settings):
    """Record one scripted run (with a human approval) into tmp_path/cassettes/test.json."""
    reset_sandbox("off")
    settings.extra["today"] = "2026-10-11"
    recording = RecordingProvider(_pay_invoice_2(sandbox_url), tmp_path / "cassettes", settings.start_url)
    human = ScriptedHumanChannel([ScriptedRule("Submit for payment", approved=True, answer="go ahead")])
    result, _ = run_worker(recording, human, task="Pay invoice 2")
    assert result.outcome == "completed_verified"
    assert _invoice(sandbox_state, 2)["status"] == "Submitted for payment"
    path = tmp_path / "cassettes" / "test.json"  # a directory target gets <run_id>.json
    return path, result


def test_cassette_records_turns_and_context(cassette):
    path, _ = cassette
    c = json.loads(path.read_text(encoding="utf-8"))
    assert c["provider"] == "scripted" and c["task"] == "Pay invoice 2" and c["today"] == "2026-10-11"
    assert c["sandbox"]["chaos"] == "off" and c["sandbox"]["state_sha"]
    assert [cv["role"] for cv in c["conversations"]] == ["worker", "verifier"]
    assert len(c["conversations"][0]["turns"]) == 6 and len(c["conversations"][1]["turns"]) == 2
    assert c["human"] == [{"approved": True, "answer": "go ahead"}]


def test_replay_reproduces_the_run(cassette, run_worker, reset_sandbox, sandbox_state, tmp_path, settings):
    path, recorded = cassette
    reset_sandbox("off")
    settings.extra = {}
    replay_settings = settings.__class__(**{**vars(settings), "provider": "replay", "cassette": str(path)})
    provider = make_provider(replay_settings)  # the same route as WORKER_PROVIDER=replay
    assert isinstance(provider, ReplayProvider) and provider.model == "replay(scripted)"
    assert replay_settings.extra["today"] == "2026-10-11"
    human = ReplayHumanChannel(provider.cassette["human"])

    result, rec = run_worker(provider, human, task=provider.cassette["task"], runs_dir=tmp_path / "replay",
                             extra=replay_settings.extra)
    assert result.outcome == recorded.outcome == "completed_verified"
    assert result.steps == recorded.steps and result.approvals == recorded.approvals
    assert result.provider == "replay"
    assert _invoice(sandbox_state, 2)["status"] == "Submitted for payment"
    assert provider.divergences == [], "the replayed model should see exactly what the recorded one saw"


def test_truncated_cassette_fails_fast(cassette, run_worker, reset_sandbox, sandbox_state, tmp_path):
    path, _ = cassette
    c = json.loads(path.read_text(encoding="utf-8"))
    c["conversations"][0]["turns"] = c["conversations"][0]["turns"][:2]  # stops after typing the username
    reset_sandbox("off")

    result, _ = run_worker(ReplayProvider(c), ReplayHumanChannel(c["human"]), task=c["task"],
                           runs_dir=tmp_path / "truncated")
    assert result.outcome == "error"
    assert "Replay diverged" in result.summary and "worker conversation #1, turn 3" in result.summary
    assert _invoice(sandbox_state, 2)["status"] == "Pending"


def test_missing_conversation_is_fatal():
    convo = ReplayProvider({"version": 1, "model": "m", "conversations": []}).conversation("sys", [])
    with pytest.raises(LLMError) as exc:
        asyncio.new_event_loop().run_until_complete(convo.complete())
    assert exc.value.fatal and "Replay diverged" in str(exc.value)
