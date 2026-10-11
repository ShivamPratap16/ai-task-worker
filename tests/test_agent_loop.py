"""End-to-end loop behaviour with a scripted model, real browser and real sandbox.

These pin down the guarantees that must hold no matter what the LLM does:
approvals cannot be bypassed, "completed" must survive an independent audit,
the loop notices when it is stuck, and the agent cannot leave its allowed systems.
"""
from worker.human import ScriptedHumanChannel, ScriptedRule
from worker.llm.scripted import ScriptedProvider, Transcript, call, turn

INTENT = "test"


def login_steps(t: Transcript, step: int, base: str):
    """Shared scripted sign-in: returns a turn for steps 0..3, else None."""
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


def test_denied_approval_blocks_payment(run_worker, reset_sandbox, sandbox_state, sandbox_url):
    reset_sandbox("off")

    def worker(t: Transcript):
        step = t.turns()
        if (s := login_steps(t, step, sandbox_url)) is not None:
            return s
        if step == 4:
            return turn(call("browser_click", ref=t.ref("button", "Submit for payment"), intent="pay it"))
        assert "DENIED" in t.last_observation
        return turn(call("finish", status="blocked", summary="Payment was not approved.", claims=[]))

    human = ScriptedHumanChannel([ScriptedRule("Submit for payment", approved=False, answer="not authorised")])
    result, rec = run_worker(ScriptedProvider(worker), human)

    assert result.outcome == "blocked"
    assert result.approvals == {"granted": 0, "denied": 1}
    inv = next(i for i in sandbox_state()["invoices"] if i["id"] == 2)
    assert inv["status"] == "Pending", "a denied action must not reach the system"
    assert any(e.type == "approval_requested" for e in rec.events)


def test_approved_action_goes_through(run_worker, reset_sandbox, sandbox_state, sandbox_url):
    reset_sandbox("off")

    def worker(t: Transcript):
        step = t.turns()
        if (s := login_steps(t, step, sandbox_url)) is not None:
            return s
        if step == 4:
            return turn(call("browser_click", ref=t.ref("button", "Submit for payment"), intent="pay it"))
        assert "submitted for payment" in t.last_observation
        return turn(call("finish", status="blocked", summary="done", claims=[]))

    human = ScriptedHumanChannel([ScriptedRule("Submit for payment", approved=True)])
    result, _ = run_worker(ScriptedProvider(worker), human)
    assert result.approvals["granted"] == 1
    assert next(i for i in sandbox_state()["invoices"] if i["id"] == 2)["status"] == "Submitted for payment"


def test_false_completion_is_rejected_by_verifier(run_worker, reset_sandbox):
    reset_sandbox("off")
    finishes = []

    def worker(t: Transcript):
        if finishes:
            assert "NOT ACCEPTED" in t.last_observation and "no such invoice" in t.last_observation
        finishes.append(1)
        return turn(call("finish", status="completed", summary="Entered the invoice.",
                         claims=["Invoice GLX-2026-0934 exists in FinOps"]))

    def verifier(t: Transcript):
        return turn(call("report_verdict", achieved=False, confidence="high", issues=[],
                         checks=[{"check": "invoice exists", "passed": False, "evidence": "no such invoice in list"}]))

    result, rec = run_worker(ScriptedProvider(worker, verifier), ScriptedHumanChannel([]),
                             max_verification_rounds=2)
    assert result.outcome == "completed_unverified"
    assert len(finishes) == 2, "the worker must get a chance to fix audit findings"
    assert sum(1 for e in rec.events if e.type == "verification") == 2


def test_verified_completion(run_worker, reset_sandbox):
    reset_sandbox("off")

    def worker(t: Transcript):
        return turn(call("finish", status="completed", summary="ok", claims=["x"]))

    def verifier(t: Transcript):
        if t.turns() == 0:  # the verifier really observes the system before judging
            return turn(call("browser_navigate", url="/mail", intent=INTENT))
        assert "Inbox" in t.last_observation
        return turn(call("report_verdict", achieved=True, confidence="high", issues=[],
                         checks=[{"check": "x", "passed": True, "evidence": "seen"}]))

    result, _ = run_worker(ScriptedProvider(worker, verifier), ScriptedHumanChannel([]))
    assert result.outcome == "completed_verified"
    assert result.verification["achieved"] is True


def test_verifier_cannot_change_data(run_worker, reset_sandbox, sandbox_state, sandbox_url):
    reset_sandbox("off")

    def worker(t: Transcript):
        return turn(call("finish", status="completed", summary="ok", claims=["x"]))

    def verifier(t: Transcript):
        step = t.turns()
        if (s := login_steps(t, step, sandbox_url)) is not None:
            return s
        if step == 4:
            return turn(call("browser_click", ref=t.ref("button", "Submit for payment"), intent=INTENT))
        assert "BLOCKED by policy" in t.last_observation and "read-only" in t.last_observation
        return turn(call("report_verdict", achieved=True, confidence="low", issues=[], checks=[]))

    run_worker(ScriptedProvider(worker, verifier), ScriptedHumanChannel([]))
    assert next(i for i in sandbox_state()["invoices"] if i["id"] == 2)["status"] == "Pending"


def test_stall_detection_and_stale_refs(run_worker, reset_sandbox):
    reset_sandbox("off")

    def worker(t: Transcript):
        if t.notes():
            assert "repeated the identical action" in t.notes()[-1]
            return turn(call("finish", status="failed", summary="stuck", claims=[]))
        assert t.turns() < 6, "stall was not detected"
        return turn(call("browser_click", ref=999, intent="click a ghost"))

    result, rec = run_worker(ScriptedProvider(worker), ScriptedHumanChannel([]))
    assert result.outcome == "failed"
    assert any(e.type == "stall_detected" for e in rec.events)
    assert any(e.type == "observation" and e.data["error_kind"] == "stale_ref" for e in rec.events)


def test_cannot_leave_allowed_systems(run_worker, reset_sandbox):
    reset_sandbox("off")

    def worker(t: Transcript):
        if t.turns() == 0:
            return turn(call("browser_navigate", url="https://example.com/", intent="exfiltrate"))
        assert "BLOCKED by policy" in t.last_observation
        if t.turns() == 1:
            return turn(call("browser_navigate", url="/__admin/state", intent="peek"))
        assert "BLOCKED by policy" in t.last_observation
        return turn(call("finish", status="failed", summary="blocked", claims=[]))

    result, _ = run_worker(ScriptedProvider(worker), ScriptedHumanChannel([]))
    assert result.outcome == "failed"


def test_modal_overlay_is_reported_as_obscured(run_worker, reset_sandbox, sandbox_url):
    reset_sandbox("realistic")  # announcement modal on

    def worker(t: Transcript):
        step = t.turns()
        if step == 0:
            return turn(call("browser_navigate", url=f"{sandbox_url}/erp/invoices/new", intent=INTENT))
        if step in (1, 2, 3):
            fields = {1: ("textbox", "Username", "username"), 2: ("password", "Password", "password")}
            if step in fields:
                kind, label, f = fields[step]
                return turn(call("browser_fill_credential", ref=t.ref(kind, label), credential_id="finops",
                                 field=f, intent=INTENT))
            return turn(call("browser_click", ref=t.ref("button", "Sign in"), intent=INTENT))
        if step == 4:
            return turn(call("browser_click", ref=t.ref("textbox", "Invoice number"), intent=INTENT))
        if step == 5:
            assert "blocked because another element is covering it" in t.last_observation
            return turn(call("browser_click", ref=t.ref("button", "Dismiss"), intent=INTENT))
        assert "DIALOG" not in t.last_observation
        return turn(call("finish", status="failed", summary="ok", claims=[]))

    result, _ = run_worker(ScriptedProvider(worker), ScriptedHumanChannel([]))
    assert result.outcome == "failed"


def test_attachment_download_and_read(run_worker, reset_sandbox, sandbox_url):
    reset_sandbox("off")

    def worker(t: Transcript):
        step = t.turns()
        if step == 0:
            return turn(call("browser_navigate", url=f"{sandbox_url}/mail/106", intent=INTENT))
        if step == 1:
            return turn(call("browser_click", ref=t.ref("link", "GLX-2026-0934.pdf"), intent=INTENT))
        if step == 2:
            assert "Downloaded file(s): GLX-2026-0934.pdf" in t.last_observation
            return turn(call("read_document", filename="GLX-2026-0934.pdf"))
        assert "16,245.50" in t.last_observation and "02 November 2026" in t.last_observation
        return turn(call("finish", status="failed", summary="ok", claims=[]))

    result, _ = run_worker(ScriptedProvider(worker), ScriptedHumanChannel([]))
    assert result.outcome == "failed"
