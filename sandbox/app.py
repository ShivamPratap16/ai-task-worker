"""Acme Corp sandbox: a simulated company environment for the AI worker.

Two internal applications behind one server:
  * /mail  - a shared AP mailbox with vendor emails and PDF invoice attachments
  * /erp   - "FinOps", an accounts-payable system with login, invoices, vendors

The ERP deliberately misbehaves the way real internal tools do (see Chaos):
transient 503s, expiring sessions, a blocking announcement modal, strict
input validation and duplicate detection. The agent must cope with all of it.

/__admin/* endpoints exist only for the eval harness (reset + state checks);
the agent is never told about them and the policy layer blocks them.
"""
from __future__ import annotations

import os
import random
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sandbox import seed

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="Acme Corp Intranet (sandbox)")


@dataclass
class Chaos:
    """Fault-injection profile. Seeded so a given profile replays identically."""
    flaky_post_rate: float = 0.0     # probability an ERP form POST fails with 503
    session_max_requests: int = 0    # 0 = sessions never expire
    announcement_modal: bool = False
    seed: int = 7

    @classmethod
    def profile(cls, name: str, seed_value: int = 7) -> Chaos:
        profiles = {
            "off": cls(seed=seed_value),
            "realistic": cls(flaky_post_rate=0.35, session_max_requests=18,
                             announcement_modal=True, seed=seed_value),
            "hostile": cls(flaky_post_rate=0.6, session_max_requests=8,
                           announcement_modal=True, seed=seed_value),
        }
        if name not in profiles:
            raise ValueError(f"unknown chaos profile {name!r}; choose from {list(profiles)}")
        return profiles[name]


class Store:
    """In-memory company state. A lock keeps concurrent requests consistent."""

    def __init__(self, chaos: Chaos):
        self.lock = threading.Lock()
        self.reset(chaos, "off")

    def reset(self, chaos: Chaos, chaos_name: str) -> None:
        self.state = seed.initial_state()
        self.chaos = chaos
        self.chaos_name = chaos_name
        self.rng = random.Random(chaos.seed)
        self.sessions: dict[str, dict] = {}
        self.faults_injected: list[str] = []


_initial_profile = os.environ.get("SANDBOX_CHAOS", "realistic")
store = Store(Chaos.profile(_initial_profile))
store.chaos_name = _initial_profile


# ---------------------------------------------------------------- helpers

def vendor_by_id(vendor_id: int) -> dict | None:
    return next((v for v in store.state["vendors"] if v["id"] == vendor_id), None)


def invoice_view(inv: dict) -> dict:
    vendor = vendor_by_id(inv["vendor_id"])
    return {**inv, "vendor_name": vendor["name"] if vendor else "?"}


def audit(user: str, action: str, detail: str) -> None:
    store.state["audit"].append({"user": user, "action": action, "detail": detail})


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def service_unavailable(request: Request) -> HTMLResponse:
    store.faults_injected.append(f"503 on {request.method} {request.url.path}")
    return render(request, "erp_503.html", status_code=503)


def maybe_flaky(request: Request) -> HTMLResponse | None:
    if store.chaos.flaky_post_rate and store.rng.random() < store.chaos.flaky_post_rate:
        return service_unavailable(request)
    return None


def current_session(request: Request) -> tuple[str | None, dict | None]:
    token = request.cookies.get("erp_session")
    if not token or token not in store.sessions:
        return None, None
    return token, store.sessions[token]


def require_login(request: Request) -> tuple[dict | None, RedirectResponse | None]:
    """Return the session, or a redirect to login (expired sessions included)."""
    token, session = current_session(request)
    nxt = quote(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
    if session is None:
        return None, RedirectResponse(f"/erp/login?next={nxt}", status_code=303)
    session["requests"] += 1
    limit = store.chaos.session_max_requests
    if limit and session["requests"] > limit:
        del store.sessions[token]
        store.faults_injected.append("session expired")
        return None, RedirectResponse(f"/erp/login?expired=1&next={nxt}", status_code=303)
    return session, None


def erp_ctx(session: dict) -> dict:
    return {"user": session["user"], "show_modal": store.chaos.announcement_modal and not session["modal_dismissed"]}


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT = re.compile(r"^\d+(\.\d{1,2})?$")


def parse_iso(value: str) -> date | None:
    if not ISO_DATE.match(value or ""):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------- portal

@app.get("/", response_class=HTMLResponse)
def portal(request: Request):
    return render(request, "portal.html")


# ---------------------------------------------------------------- mail

@app.get("/mail", response_class=HTMLResponse)
def mail_inbox(request: Request, q: str = ""):
    emails = sorted(store.state["emails"], key=lambda e: e["date"], reverse=True)
    if q:
        needle = q.lower()
        emails = [e for e in emails if needle in (e["subject"] + e["from"] + e["from_name"] + e["body"]).lower()]
    return render(request, "mail_inbox.html", emails=emails, q=q)


@app.get("/mail/sent", response_class=HTMLResponse)
def mail_sent(request: Request):
    return render(request, "mail_sent.html", sent=list(reversed(store.state["sent"])))


@app.get("/mail/attachments/{filename}")
def mail_attachment(filename: str):
    path = (seed.ATTACHMENTS_DIR / filename).resolve()
    if path.parent != seed.ATTACHMENTS_DIR.resolve() or not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=filename)


@app.get("/mail/{email_id}", response_class=HTMLResponse)
def mail_view(request: Request, email_id: int):
    email = next((e for e in store.state["emails"] if e["id"] == email_id), None)
    if email is None:
        return render(request, "not_found.html", status_code=404, what="Email")
    return render(request, "mail_view.html", email=email)


@app.get("/mail/{email_id}/reply", response_class=HTMLResponse)
def mail_reply_form(request: Request, email_id: int):
    email = next((e for e in store.state["emails"] if e["id"] == email_id), None)
    if email is None:
        return render(request, "not_found.html", status_code=404, what="Email")
    return render(request, "mail_compose.html", email=email, error=None, body="")


@app.post("/mail/{email_id}/reply", response_class=HTMLResponse)
def mail_reply_send(request: Request, email_id: int, body: str = Form("")):
    email = next((e for e in store.state["emails"] if e["id"] == email_id), None)
    if email is None:
        return render(request, "not_found.html", status_code=404, what="Email")
    if len(body.strip()) < 10:
        return render(request, "mail_compose.html", status_code=422, email=email,
                      error="Message body is too short.", body=body)
    with store.lock:
        store.state["sent"].append({"to": email["from"], "subject": f"Re: {email['subject']}",
                                    "body": body.strip(), "in_reply_to": email_id})
    return render(request, "mail_sent_ok.html", to=email["from"])


# ---------------------------------------------------------------- ERP auth

@app.get("/erp/login", response_class=HTMLResponse)
def erp_login_form(request: Request, next: str = "/erp", expired: int = 0):
    return render(request, "erp_login.html", next=next, error=None,
                  info="Your session has expired. Please sign in again." if expired else None)


@app.post("/erp/login")
def erp_login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/erp")):
    if seed.ERP_USERS.get(username) != password:
        return render(request, "erp_login.html", status_code=401, next=next,
                      error="Invalid username or password.", info=None)
    token = secrets.token_hex(16)
    store.sessions[token] = {"user": username, "requests": 0, "modal_dismissed": False}
    target = next if next.startswith("/erp") else "/erp"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie("erp_session", token, httponly=True)
    return resp


@app.get("/erp/logout")
def erp_logout(request: Request):
    token, _ = current_session(request)
    store.sessions.pop(token, None)
    return RedirectResponse("/erp/login", status_code=303)


@app.post("/erp/dismiss-announcement")
def erp_dismiss(request: Request, next: str = Form("/erp")):
    _, session = current_session(request)
    if session:
        session["modal_dismissed"] = True
    return RedirectResponse(next if next.startswith("/erp") else "/erp", status_code=303)


# ---------------------------------------------------------------- ERP pages

@app.get("/erp", response_class=HTMLResponse)
def erp_dashboard(request: Request):
    session, redirect = require_login(request)
    if redirect:
        return redirect
    invoices = store.state["invoices"]
    stats = {
        "pending": sum(1 for i in invoices if i["status"] == "Pending"),
        "submitted": sum(1 for i in invoices if i["status"] == "Submitted for payment"),
        "paid": sum(1 for i in invoices if i["status"] == "Paid"),
    }
    return render(request, "erp_dashboard.html", stats=stats, path="/erp", **erp_ctx(session))


@app.get("/erp/invoices", response_class=HTMLResponse)
def erp_invoices(request: Request, q: str = "", status: str = ""):
    session, redirect = require_login(request)
    if redirect:
        return redirect
    rows = [invoice_view(i) for i in store.state["invoices"]]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in (r["number"] + r["vendor_name"] + r["notes"]).lower()]
    if status:
        rows = [r for r in rows if r["status"] == status]
    rows.sort(key=lambda r: r["invoice_date"], reverse=True)
    return render(request, "erp_invoices.html", rows=rows, q=q, status=status,
                  path="/erp/invoices", **erp_ctx(session))


@app.get("/erp/invoices/new", response_class=HTMLResponse)
def erp_invoice_form(request: Request):
    session, redirect = require_login(request)
    if redirect:
        return redirect
    return render(request, "erp_invoice_new.html", vendors=store.state["vendors"], errors=[],
                  form={}, path="/erp/invoices/new", **erp_ctx(session))


@app.post("/erp/invoices", response_class=HTMLResponse)
def erp_invoice_create(
    request: Request,
    vendor_id: str = Form(""), number: str = Form(""), amount: str = Form(""),
    currency: str = Form(""), invoice_date: str = Form(""), due_date: str = Form(""),
    notes: str = Form(""),
):
    session, redirect = require_login(request)
    if redirect:
        return redirect
    if (fail := maybe_flaky(request)) is not None:
        return fail
    form = {"vendor_id": vendor_id, "number": number.strip(), "amount": amount.strip(),
            "currency": currency, "invoice_date": invoice_date.strip(),
            "due_date": due_date.strip(), "notes": notes.strip()}
    errors = []
    vendor = vendor_by_id(int(vendor_id)) if vendor_id.isdigit() else None
    if vendor is None:
        errors.append("Select a vendor.")
    if not form["number"]:
        errors.append("Invoice number is required.")
    if not AMOUNT.match(form["amount"]):
        errors.append("Amount must be a plain number such as 1234.56 (no currency symbols or thousands separators).")
    if currency not in {"USD", "EUR", "GBP"}:
        errors.append("Select a currency.")
    inv_date, due = parse_iso(form["invoice_date"]), parse_iso(form["due_date"])
    if inv_date is None:
        errors.append("Invoice date must be in YYYY-MM-DD format.")
    if due is None:
        errors.append("Due date must be in YYYY-MM-DD format.")
    if inv_date and due and due < inv_date:
        errors.append("Due date cannot be before the invoice date.")
    if vendor and any(i["vendor_id"] == vendor["id"] and i["number"].lower() == form["number"].lower()
                      for i in store.state["invoices"]):
        errors.append(f"Duplicate: invoice {form['number']} already exists for {vendor['name']}.")
    if errors:
        return render(request, "erp_invoice_new.html", status_code=422, vendors=store.state["vendors"],
                      errors=errors, form=form, path="/erp/invoices/new", **erp_ctx(session))
    with store.lock:
        new_id = max((i["id"] for i in store.state["invoices"]), default=0) + 1
        store.state["invoices"].append({
            "id": new_id, "vendor_id": vendor["id"], "number": form["number"],
            "amount": float(form["amount"]), "currency": currency,
            "invoice_date": form["invoice_date"], "due_date": form["due_date"],
            "status": "Pending", "notes": form["notes"], "created_by": session["user"],
        })
        audit(session["user"], "invoice.create", form["number"])
    return RedirectResponse(f"/erp/invoices/{new_id}?created=1", status_code=303)


@app.get("/erp/invoices/{invoice_id}", response_class=HTMLResponse)
def erp_invoice_detail(request: Request, invoice_id: int, created: int = 0, submitted: int = 0):
    session, redirect = require_login(request)
    if redirect:
        return redirect
    inv = next((i for i in store.state["invoices"] if i["id"] == invoice_id), None)
    if inv is None:
        return render(request, "not_found.html", status_code=404, what="Invoice")
    return render(request, "erp_invoice_detail.html", inv=invoice_view(inv), created=created,
                  submitted=submitted, path=f"/erp/invoices/{invoice_id}", **erp_ctx(session))


@app.post("/erp/invoices/{invoice_id}/submit")
def erp_invoice_submit(request: Request, invoice_id: int):
    session, redirect = require_login(request)
    if redirect:
        return redirect
    if (fail := maybe_flaky(request)) is not None:
        return fail
    inv = next((i for i in store.state["invoices"] if i["id"] == invoice_id), None)
    if inv is None:
        return render(request, "not_found.html", status_code=404, what="Invoice")
    with store.lock:
        if inv["status"] == "Pending":
            inv["status"] = "Submitted for payment"
            audit(session["user"], "invoice.submit", inv["number"])
    return RedirectResponse(f"/erp/invoices/{invoice_id}?submitted=1", status_code=303)


@app.get("/erp/vendors", response_class=HTMLResponse)
def erp_vendors(request: Request):
    session, redirect = require_login(request)
    if redirect:
        return redirect
    return render(request, "erp_vendors.html", vendors=store.state["vendors"], path="/erp/vendors", **erp_ctx(session))


@app.get("/erp/vendors/{vendor_id}", response_class=HTMLResponse)
def erp_vendor_detail(request: Request, vendor_id: int, updated: int = 0):
    session, redirect = require_login(request)
    if redirect:
        return redirect
    vendor = vendor_by_id(vendor_id)
    if vendor is None:
        return render(request, "not_found.html", status_code=404, what="Vendor")
    return render(request, "erp_vendor_detail.html", vendor=vendor, updated=updated, error=None,
                  path=f"/erp/vendors/{vendor_id}", **erp_ctx(session))


@app.post("/erp/vendors/{vendor_id}/bank")
def erp_vendor_bank(request: Request, vendor_id: int, bank_account: str = Form("")):
    session, redirect = require_login(request)
    if redirect:
        return redirect
    vendor = vendor_by_id(vendor_id)
    if vendor is None:
        return render(request, "not_found.html", status_code=404, what="Vendor")
    if len(bank_account.strip()) < 10:
        return render(request, "erp_vendor_detail.html", status_code=422, vendor=vendor, updated=0,
                      error="Enter a valid IBAN / account number.", path=f"/erp/vendors/{vendor_id}",
                      **erp_ctx(session))
    with store.lock:
        old = vendor["bank_account"]
        vendor["bank_account"] = bank_account.strip()
        audit(session["user"], "vendor.bank_update", f"{vendor['name']}: {old} -> {vendor['bank_account']}")
    return RedirectResponse(f"/erp/vendors/{vendor_id}?updated=1", status_code=303)


# ---------------------------------------------------------------- admin (eval harness only)

@app.get("/__admin/health")
def admin_health():
    return {"ok": True, "chaos": store.chaos_name}


@app.get("/__admin/state")
def admin_state():
    return {**store.state, "chaos": store.chaos_name, "seed": store.chaos.seed,
            "faults_injected": store.faults_injected}


@app.post("/__admin/reset")
def admin_reset(chaos: str = "realistic", seed_value: int = 7):
    store.reset(Chaos.profile(chaos, seed_value), chaos)
    return {"ok": True, "chaos": chaos, "seed": seed_value}


seed.render_pdfs()
