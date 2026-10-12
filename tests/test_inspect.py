"""The terminal debugging view: a projection of trace.jsonl, so it is tested on a synthetic trace."""
import json

import pytest

from worker.inspect_run import find_run, load_events, step_detail, timeline


def ev(seq, type_, **data):
    return {"seq": seq, "ts": 1000.0 + seq, "type": type_, "data": data}


TRACE = [
    ev(0, "run_started", task="Enter the invoice", provider="scripted", model="m"),
    ev(1, "action", actor="worker", call_id="a", tool="browser_click", args={"ref": 3, "intent": "Save"},
       intent="Save"),
    ev(2, "observation", actor="worker", call_id="a", ok=True, error_kind="", url="http://x/erp",
       content="Clicked.\nWARNING: the server answered HTTP 503; the action likely did not take effect.\nPAGE"),
    ev(3, "action", actor="worker", call_id="b", tool="browser_click", args={"ref": 9}, intent="Pay"),
    ev(4, "observation", actor="worker", call_id="b", ok=False, error_kind="policy_blocked",
       content="BLOCKED by policy"),
    ev(5, "action", actor="worker", call_id="c", tool="remember", args={"key": "id", "value": "4"}, intent=""),
    ev(6, "observation", actor="worker", call_id="c", ok=True, error_kind="", content="Saved."),
    ev(7, "verification", round=1, achieved=False, confidence="high", inconclusive=False,
       checks=[{"check": "invoice exists", "passed": False, "evidence": "not found"}], issues=[]),
    ev(8, "run_finished", outcome="failed", steps=3, duration_s=8.0),
]


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "eval-demo-1200"
    d.mkdir(parents=True)
    (d / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in TRACE), encoding="utf-8")
    return d


def test_timeline_numbers_actions_and_flags_server_errors(run_dir):
    lines = timeline(load_events(run_dir))
    assert any("#1" in line and "ok, but WARNING: the server answered HTTP 503" in line for line in lines)
    assert any("#2" in line and "ERR[policy_blocked]" in line for line in lines)
    assert any("FAIL: invoice exists" in line for line in lines)
    assert lines[-1].endswith("=== failed after 3 steps, 8.0s ===")


def test_failures_view_hides_healthy_steps(run_dir):
    text = "\n".join(timeline(load_events(run_dir), failures_only=True))
    assert "#1" in text and "#2" in text and "#3" not in text and "remember" not in text


def test_step_detail_shows_what_the_model_saw(run_dir):
    detail = step_detail(load_events(run_dir), 1)
    assert "intent:  Save" in detail and "url:     http://x/erp" in detail and detail.endswith("PAGE")
    with pytest.raises(SystemExit):
        step_detail(load_events(run_dir), 9)


def test_find_run_by_latest_and_prefix(run_dir):
    runs = run_dir.parent
    assert find_run(runs, "latest") == run_dir
    assert find_run(runs, "eval-demo") == run_dir
    with pytest.raises(SystemExit):
        find_run(runs, "nope")
