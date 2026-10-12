"""Operator controls: stopping a run, steering it, reviewing its plan, history, quota handling, API hardening."""
import json
import threading
import time

import httpx
import openai


from worker.human import ScriptedHumanChannel, WebHumanChannel
from worker.llm.scripted import ScriptedProvider, Transcript, call, turn

INTENT = "test"


def _login(t: Transcript, step: int, base: str):
    if step == 0:
        return turn(call("browser_navigate", url=f"{base}/erp/invoices/2", intent=INTENT))
    if step == 1:
        return turn(call("browser_fill_credential", ref=t.ref("textbox", "Username"), credential_id="finops",
                         field="username", intent=INTENT))
    if step == 2:
        return turn(call("browser_fill_credential", ref=t.ref("password", "Password"), credential_id="finops",
                         field="password", intent=INTENT))
    if step == 3:
        return turn(call("browser_click", ref=t.ref("button", "Sign in"), intent=INTENT))
    return None


def _run(settings, provider, human, cancel=None):
    from worker.agent import Worker
    from worker.events import RunRecorder
    from worker.runner import new_event_loop
    recorder = RunRecorder("test", settings.runs_dir / "test")
    loop = new_event_loop()
    try:
        result = loop.run_until_complete(Worker(settings, provider).run("test task", recorder, human, cancel))
    finally:
        loop.close()
        recorder.close()
    return result, recorder


def test_stop_releases_a_pending_approval(settings, reset_sandbox, sandbox_state, sandbox_url):
    """Stopping must not wait out the 10-minute approval timeout, and the gated action must not happen."""
    reset_sandbox("off")

    def worker(t: Transcript):
        if (s := _login(t, t.turns(), sandbox_url)) is not None:
            return s
        return turn(call("browser_click", ref=t.ref("button", "Submit for payment"), intent="pay it"))

    human, cancel = WebHumanChannel(), threading.Event()

    def operator_presses_stop():
        while not human.pending():
            time.sleep(0.05)
        cancel.set()
        human.close()

    threading.Thread(target=operator_presses_stop, daemon=True).start()
    started = time.time()
    result, _ = _run(settings, ScriptedProvider(worker), human, cancel)
    assert result.outcome == "cancelled"
    assert time.time() - started < 30
    assert next(i for i in sandbox_state()["invoices"] if i["id"] == 2)["status"] != "Submitted for payment"


def test_operator_message_reaches_the_worker(settings, reset_sandbox):
    reset_sandbox("off")
    seen = []

    def worker(t: Transcript):
        if t.turns() == 0:
            return turn(call("remember", key="k", value="v"))
        seen.extend(t.notes())
        return turn(call("finish", status="blocked", summary="stopped as asked", claims=[]))

    human = WebHumanChannel()
    human.post_message("Use the Globex Corp vendor record.")
    result, _ = _run(settings, ScriptedProvider(worker), human)
    assert result.outcome == "blocked"
    assert any("Use the Globex Corp vendor record." in n and "OPERATOR" in n for n in seen)


def test_plan_review_gates_actions_until_approved(settings, reset_sandbox, sandbox_url):
    reset_sandbox("off")
    settings.plan_review = True
    observations = []

    def worker(t: Transcript):
        step = t.turns()
        if step > 0:
            observations.append(t.last_observation)
        if step == 0:
            return turn(call("browser_navigate", url=f"{sandbox_url}/mail", intent="act before planning"))
        if step == 1:
            return turn(call("update_plan", steps=[{"step": "Read the mail", "status": "pending"}]))
        if step == 2:
            return turn(call("update_plan", steps=[{"step": "Read the mail", "status": "pending"},
                                                   {"step": "Check FinOps", "status": "pending"}]))
        if step == 3:
            return turn(call("browser_navigate", url=f"{sandbox_url}/mail", intent="now allowed"))
        return turn(call("finish", status="blocked", summary="done", claims=[]))

    approvals = iter([False, True])

    class ReviewingHuman(ScriptedHumanChannel):
        async def ask(self, request, timeout_s):
            from worker.human import HumanResponse
            assert request.kind == "plan_review"
            ok = next(approvals)
            return HumanResponse(answer="approved" if ok else "also check FinOps", approved=ok, responder="test")

    _run(settings, ScriptedProvider(worker), ReviewingHuman([]))
    assert "Plan review is on" in observations[0]
    assert "asked for changes: also check FinOps" in observations[1]
    assert "APPROVED" in observations[2]
    assert "Navigated to" in observations[3]


def test_history_survives_a_restart(settings, run_worker, reset_sandbox):
    reset_sandbox("off")
    from worker.runner import RunManager
    run_worker(ScriptedProvider(lambda t: turn(call("finish", status="blocked", summary="nothing to do", claims=[]))),
               ScriptedHumanChannel([]))
    # A run whose server died mid-way: trace without run_finished.
    broken = settings.runs_dir / "broken"
    broken.mkdir(parents=True)
    (broken / "trace.jsonl").write_text(json.dumps({"seq": 0, "ts": 1.0, "type": "run_started",
                                                    "data": {"task": "half done"}}) + "\n", encoding="utf-8")
    manager = RunManager(settings)
    manager.load_history()
    assert manager.get("test").summary()["outcome"] == "blocked"
    interrupted = manager.get("broken")
    assert interrupted.summary()["outcome"] == "error"
    assert interrupted.recorder.events[-1].type == "run_finished"


GEMINI_429 = ("Error code: 429 - [{'error': {'code': 429, 'message': 'You exceeded your current quota. \\n* Quota "
              "exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, "
              "model: gemini-3.6-flash\\nPlease retry in 5h1m12.58s.', 'details': [{'quotaId': "
              "'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}, {'retryDelay': '18072s'}]}}]")


def test_parse_quota_error():
    from worker.quota import parse_quota_error
    info = parse_quota_error(GEMINI_429)
    assert info["limit"] == 20 and info["quota_model"] == "gemini-3.6-flash" and info["retry_s"] == 18072


def test_daily_quota_switches_to_fallback_model(tmp_path):
    from worker.llm.openai_provider import OpenAIProvider
    from worker.quota import QuotaLedger
    ledger = QuotaLedger(tmp_path / "quota.json")
    provider = OpenAIProvider("big-model", name="gemini", api_key="x", base_url="http://unused/",
                              fallback_models=["small-model"], ledger=ledger)
    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "big-model":
            raise openai.RateLimitError(GEMINI_429, response=httpx.Response(
                429, request=httpx.Request("POST", "http://unused/")), body=None)
        return "ok"

    provider.client.chat.completions.create = fake_create
    messages = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"},
         "extra_content": {"google": {"thought_signature": "from-big-model"}}}]}]
    import asyncio
    assert asyncio.run(provider.create(messages, [])) == "ok"
    assert calls == ["big-model", "small-model"] and provider.model == "small-model"
    notice = provider.drain_notices()[0]
    assert notice["to"] == "small-model" and "20 requests/day" in notice["reason"] and "5h" in notice["reason"]
    assert messages[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "skip_thought_signature_validator"
    assert ledger.exhausted("big-model") and ledger.status("small-model")["requests"] == 1
    # A new run skips the exhausted model from the start.
    again = OpenAIProvider("big-model", name="gemini", api_key="x", base_url="http://unused/",
                           fallback_models=["small-model"], ledger=ledger)
    assert again.model == "small-model"


def test_api_refuses_cross_origin_writes():
    from fastapi.testclient import TestClient
    from worker.server import app
    client = TestClient(app)
    evil = client.post("/api/runs", json={"task": ""}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    same = client.post("/api/runs", json={"task": ""}, headers={"Origin": "http://testserver"})
    assert same.status_code == 400  # passed the origin check, rejected for the empty task
