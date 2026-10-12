"""The dashboard end to end: real server, real SSE, real sandbox and browser, scripted model (no API cost).

A headless browser drives the UI the way an operator would: start a task with plan review on, approve
the plan, approve a risky action (with its screenshot), message the worker mid-run, answer its
question, and read the verified result.
"""
import asyncio
import os
import threading
import time

import httpx
import pytest
import uvicorn

from worker.llm.scripted import ScriptedProvider, Transcript, call, turn

PORT = int(os.environ.get("TEST_DASHBOARD_PORT", "8012"))
STEER = "Use the Globex Corp vendor record."


@pytest.fixture
def dashboard(settings, sandbox_url, reset_sandbox, monkeypatch):
    import worker.runner as runner
    import worker.server as server
    from worker.agent import Worker
    reset_sandbox("off")
    heard = []

    def brain(t: Transcript):
        s = t.turns()
        if s == 0:
            return turn(call("update_plan", steps=[{"step": "Sign in to FinOps", "status": "in_progress"},
                                                   {"step": "Submit invoice 2", "status": "pending"}]),
                        text="Planning first.")
        if s == 1:
            return turn(call("browser_navigate", url=f"{sandbox_url}/erp/invoices/2", intent="Open invoice 2"))
        if s == 2:
            return turn(call("browser_fill_credential", ref=t.ref("textbox", "Username"), credential_id="finops",
                             field="username", intent="Username"))
        if s == 3:
            return turn(call("browser_fill_credential", ref=t.ref("password", "Password"), credential_id="finops",
                             field="password", intent="Password"))
        if s == 4:
            return turn(call("browser_click", ref=t.ref("button", "Sign in"), intent="Sign in"))
        if s == 5:
            return turn(call("browser_click", ref=t.ref("button", "Submit for payment"), intent="Submit invoice 2"))
        if s == 6:
            return turn(call("ask_human", kind="clarification", question="Email the vendor too?", options=["Yes", "No"]))
        heard.extend(t.notes())
        return turn(call("finish", status="completed", summary="Invoice 2 submitted for payment.",
                         claims=["Invoice 2 is Submitted for payment"], evidence=[f"{sandbox_url}/erp/invoices/2"]))

    def verifier(t: Transcript):
        if t.turns() == 0:
            return turn(call("browser_navigate", url="/mail", intent="Look at the live system"))
        return turn(call("report_verdict", achieved=True, confidence="high", issues=[],
                         checks=[{"check": "Invoice 2 submitted", "passed": True, "evidence": "/erp/invoices/2"}]))

    monkeypatch.setattr(runner, "Worker", lambda s: Worker(s, ScriptedProvider(brain, verifier)))
    manager = runner.RunManager(settings)
    monkeypatch.setattr(server, "manager", manager)
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{PORT}", heard
    for run_id, handle in list(manager.runs.items()):  # a failed assertion must not leave a run waiting on us
        manager.cancel(run_id)
        handle.thread and handle.thread.join(timeout=15)
    srv.should_exit = True
    thread.join(timeout=5)


async def _drive(base: str) -> dict:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = None
        for channel in (None, "chrome", "msedge"):
            try:
                browser = await p.chromium.launch(channel=channel)
                break
            except Exception:
                continue
        assert browser, "no browser available"
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(base)
        await page.fill("#task", "Submit invoice 2 for payment")
        await page.click("label:has(#planReview)")
        assert await page.is_checked("#planReview")
        await page.click("#start")

        await page.wait_for_selector(".card.ask >> text=Review the plan", timeout=30000)
        assert await page.locator(".card.ask ol li").count() == 2
        await page.click(".card.ask button:has-text('Request changes')")  # with no text: refused client-side
        assert await page.locator(".card.ask").count() == 1
        await page.click(".card.ask button:has-text('Approve plan')")

        await page.wait_for_selector(".card.ask >> text=Approval needed", timeout=30000)
        assert await page.locator(".card.ask img.shot").count() == 1
        assert "(1) Needs you" in await page.title()
        await page.click(".card.ask button:has-text('Approve')")

        await page.wait_for_selector(".card.ask >> text=Email the vendor too?", timeout=30000)
        await page.fill("#steerInput", STEER)
        await page.click("#steer button")
        await page.wait_for_selector(f".ev.k-you >> text={STEER}", timeout=10000)
        await page.click(".card.ask button:has-text('No')")

        await page.wait_for_selector(".result.ok", timeout=60000)
        out = {
            "pill": await page.inner_text(".run-head .pill"),
            "plan": await page.inner_text("#planCount"),
            "steer_hidden": await page.locator("#steer").is_hidden(),
            "report_href": await page.get_attribute(".result a[download]", "href"),
            "errors": errors,
        }
        # Trace waterfall, flow canvas and the step inspector.
        await page.click(".tabs button:has-text('Trace')")
        out["trace_rows"] = await page.locator(".trace-grid .tr").count()
        await page.click(".tabs button:has-text('Flow')")
        out["flow_nodes"] = await page.locator(".fnode").count()
        await page.click(".tabs button:has-text('Activity')")
        await page.hover(".ev.k-action .box >> nth=0")
        await page.click(".ev .inspect >> nth=0")
        out["drawer"] = await page.inner_text(".drawer .dr-title")
        await page.keyboard.press("Escape")
        # Overview dashboard lists the run.
        await page.click("#navOverview")
        await page.wait_for_selector(".runs-tbl tbody tr", timeout=10000)
        out["overview_rows"] = await page.locator(".runs-tbl tbody tr").count()
        out["errors"] = errors
        await browser.close()
        return out


def test_dashboard_end_to_end(dashboard, sandbox_state):
    base, heard = dashboard
    out = asyncio.run(_drive(base))
    assert out["errors"] == []
    assert out["pill"] == "Completed & verified"
    assert out["plan"] == "2 of 2"          # closed on verification
    assert out["steer_hidden"]               # message box only while running
    assert out["trace_rows"] >= 2 + 5        # axis + run root + every tool step
    assert out["flow_nodes"] == 2 + 3        # trigger, 2 plan steps, audit, result
    assert out["drawer"] == "Navigate"
    assert out["overview_rows"] == 1
    assert any(STEER in n for n in heard)    # the mid-run message reached the worker
    assert next(i for i in sandbox_state()["invoices"] if i["id"] == 2)["status"] == "Submitted for payment"
    report = httpx.get(base + out["report_href"])
    assert report.status_code == 200 and "completed_verified" in report.text
