"""The sandbox must behave like a real (imperfect) internal system, deterministically."""
from fastapi.testclient import TestClient

from sandbox.app import app, store

client = TestClient(app)


def reset(chaos="off"):
    client.post("/__admin/reset", params={"chaos": chaos})


def login(c: TestClient):
    r = c.post("/erp/login", data={"username": "ap.agent", "password": "Acme!2026", "next": "/erp"},
               follow_redirects=False)
    assert r.status_code == 303


VALID = {"vendor_id": "1", "number": "GLX-2026-0934", "amount": "16245.50", "currency": "USD",
         "invoice_date": "2026-10-03", "due_date": "2026-11-02", "notes": ""}


def test_erp_requires_login():
    reset()
    r = TestClient(app).get("/erp/invoices", follow_redirects=False)
    assert r.status_code == 303 and "/erp/login" in r.headers["location"]


def test_create_invoice_and_reject_duplicate():
    reset()
    c = TestClient(app)
    login(c)
    r = c.post("/erp/invoices", data=VALID, follow_redirects=False)
    assert r.status_code == 303
    assert any(i["number"] == "GLX-2026-0934" for i in store.state["invoices"])
    r = c.post("/erp/invoices", data=VALID)
    assert r.status_code == 422 and "Duplicate" in r.text


def test_validation_rejects_human_formats():
    reset()
    c = TestClient(app)
    login(c)
    bad = {**VALID, "amount": "$16,245.50", "due_date": "02 November 2026"}
    r = c.post("/erp/invoices", data=bad)
    assert r.status_code == 422
    assert "plain number" in r.text and "YYYY-MM-DD" in r.text


def test_chaos_is_deterministic_and_injects_503():
    def statuses():
        reset("hostile")
        c = TestClient(app)
        login(c)
        out = []
        for n in range(6):
            r = c.post("/erp/invoices", data={**VALID, "number": f"T-{n}"}, follow_redirects=False)
            out.append(r.status_code)
            if r.status_code == 303 and "login" in r.headers["location"]:
                login(c)
        return out
    first, second = statuses(), statuses()
    assert first == second, "same seed must replay identically"
    assert 503 in first


def test_session_expiry_redirects_to_login():
    reset("hostile")  # session_max_requests=8
    c = TestClient(app)
    login(c)
    codes = [c.get("/erp/invoices", follow_redirects=False) for _ in range(9)]
    assert "expired=1" in codes[-1].headers.get("location", "")


def test_attachment_download_and_path_traversal():
    reset()
    assert client.get("/mail/attachments/GLX-2026-0934.pdf").headers["content-type"] == "application/pdf"
    assert client.get("/mail/attachments/..%2Fseed.py").status_code == 404
