"""The eval harness must not score provider trouble as agent failure, nor confuse an outage with a used-up quota."""
from evals.run_evals import _provider_outage, _verdict

UNAVAILABLE = "The language model was unavailable: "


def test_outage_quota_and_real_failures_are_told_apart():
    assert _provider_outage({"outcome": "error", "summary": UNAVAILABLE + "gemini API error: Connection error."}) \
        == "outage"
    assert _provider_outage({"outcome": "error", "summary": UNAVAILABLE + "the quota for m is used up"}) == "quota"
    assert _provider_outage({"outcome": "error", "summary": "Internal error: KeyError"}) is None
    assert _provider_outage({"outcome": "failed", "summary": "Step budget exhausted"}) is None


def test_verdict_labels():
    assert _verdict({"passed": True, "provider_outage": None}) == "PASS"
    assert _verdict({"passed": False, "provider_outage": "outage"}) == "OUTAGE"
    assert _verdict({"passed": False, "provider_outage": "quota"}) == "NO QUOTA"
    assert _verdict({"passed": False, "provider_outage": None}) == "FAIL"
