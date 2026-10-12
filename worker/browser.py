"""Browser control for the agent (Playwright, Chromium).

Perception: instead of screenshots, the model reads a compact *text snapshot*
of the page in document order: headings, text, table rows, alerts/dialogs, and
every visible interactive element tagged with a short numeric ref, e.g.

    [7] textbox "Invoice date" value="" placeholder="YYYY-MM-DD"
    [9] button "Save invoice"

Actions address elements by ref. Refs are re-assigned on every snapshot, and
every action returns a fresh snapshot, so the model always acts on what it just
saw. This is site-agnostic (nothing here knows about the sandbox) and far
cheaper and more deterministic than pixel-based control; screenshots are still
captured after every action as human-facing evidence.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Download, Page, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
import contextlib

SNAPSHOT_CHAR_LIMIT = 14000

SNAPSHOT_JS = r"""
() => {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','HEAD']);
  const out = []; let line = [];
  let ref = 0;
  document.querySelectorAll('[data-agent-ref]').forEach(e => e.removeAttribute('data-agent-ref'));
  const flush = () => { const s = line.join(' ').replace(/\s+/g, ' ').trim(); if (s) out.push(s); line = []; };
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    if (el.offsetParent === null && st.position !== 'fixed' && el.tagName !== 'BODY' && el.tagName !== 'OPTION') {
      const r = el.getBoundingClientRect(); if (r.width === 0 && r.height === 0) return false;
    }
    return true;
  };
  const labelFor = (el) => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
    const wrap = el.closest('label'); if (wrap) return wrap.innerText.trim();
    return el.getAttribute('placeholder') || el.getAttribute('name') || el.getAttribute('title') || '';
  };
  const q = (s) => JSON.stringify((s || '').replace(/\s+/g, ' ').trim().slice(0, 120));
  const isInteractive = (el) => {
    const t = el.tagName;
    if (t === 'A' && el.hasAttribute('href')) return true;
    if (['BUTTON','SELECT','TEXTAREA'].includes(t)) return true;
    if (t === 'INPUT') return el.type !== 'hidden';
    const role = el.getAttribute('role');
    return role === 'button' || role === 'link' || role === 'checkbox' || role === 'tab' || el.hasAttribute('onclick');
  };
  const describe = (el) => {
    ref += 1; el.setAttribute('data-agent-ref', String(ref));
    const t = el.tagName; const dis = el.disabled ? ' (disabled)' : '';
    if (t === 'A') {
      const href = el.getAttribute('href');
      const dl = el.hasAttribute('download') ? ' (download)' : '';
      return `[${ref}] link ${q(el.innerText || el.getAttribute('aria-label') || href)} -> ${href}${dl}`;
    }
    if (t === 'SELECT') {
      const opts = [...el.options].map(o => o.text.trim()).filter(Boolean);
      const sel = el.selectedIndex >= 0 ? el.options[el.selectedIndex].text.trim() : '';
      return `[${ref}] combobox ${q(labelFor(el))} selected=${q(sel)} options=${JSON.stringify(opts)}${dis}`;
    }
    if (t === 'TEXTAREA') return `[${ref}] textarea ${q(labelFor(el))} value=${q(el.value)}${dis}`;
    if (t === 'INPUT') {
      const type = (el.type || 'text').toLowerCase();
      if (['submit','button','reset','image'].includes(type)) return `[${ref}] button ${q(el.value || labelFor(el))}${dis}`;
      if (type === 'checkbox' || type === 'radio') return `[${ref}] ${type} ${q(labelFor(el))} checked=${el.checked}${dis}`;
      const val = type === 'password' ? (el.value ? '"********"' : '""') : q(el.value);
      const ph = el.getAttribute('placeholder') ? ` placeholder=${q(el.getAttribute('placeholder'))}` : '';
      return `[${ref}] ${type === 'password' ? 'password' : 'textbox'} ${q(labelFor(el))} value=${val}${ph}${dis}`;
    }
    return `[${ref}] button ${q(el.innerText || el.getAttribute('aria-label') || el.value)}${dis}`;
  };
  const BLOCK = new Set(['block','flex','grid','table','table-row','list-item','table-caption','flow-root']);
  const walk = (node) => {
    if (node.nodeType === Node.TEXT_NODE) { const s = node.textContent.trim(); if (s) line.push(s); return; }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node; if (SKIP.has(el.tagName) || !visible(el)) return;
    if (isInteractive(el)) { line.push(describe(el)); return; }
    const disp = getComputedStyle(el).display;
    const block = BLOCK.has(disp) || /^H[1-6]$/.test(el.tagName);
    if (block) flush();
    const role = el.getAttribute('role');
    if (/^H[1-6]$/.test(el.tagName)) line.push('#'.repeat(Number(el.tagName[1])));
    if (role === 'alert') line.push('[ALERT]');
    if (role === 'status') line.push('[STATUS]');
    if (role === 'dialog' || el.getAttribute('aria-modal') === 'true') { flush(); out.push(`[DIALOG ${q(el.getAttribute('aria-label') || '')} - modal overlay; it may block clicks on the page behind it]`); }
    if (disp === 'table-cell' && line.length) line.push('|');
    if (el.tagName === 'LI') line.push('-');
    for (const child of el.childNodes) walk(child);
    if (block) flush();
  };
  walk(document.body); flush();
  return { title: document.title, url: location.href, text: out.join('\n'), refs: ref };
}
"""  # noqa: E501  (JavaScript run in the page)


class BrowserActionError(Exception):
    """An action failed in a way the agent should see and react to."""

    def __init__(self, message: str, kind: str = "action_failed", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass
class Snapshot:
    url: str
    title: str
    text: str
    http_status: int | None
    refs: int

    def render(self, limit: int = SNAPSHOT_CHAR_LIMIT) -> str:
        status = f" | HTTP {self.http_status}" if self.http_status else ""
        body = self.text
        if len(body) > limit:
            body = body[:limit] + f"\n... [snapshot truncated at {limit} chars]"
        return f"URL: {self.url}{status}\nTITLE: {self.title}\n---\n{body}"


@dataclass
class BrowserSession:
    downloads_dir: Path
    screenshots_dir: Path
    headless: bool = True
    _pw: object = None
    _browser: Browser | None = None
    _context: BrowserContext | None = None
    page: Page | None = None
    last_status: int | None = None
    new_downloads: list[Path] = field(default_factory=list)
    _shot_counter: int = 0
    _owns_browser: bool = True

    async def start(self, browser: Browser | None = None) -> None:
        if browser is None:
            self._pw = await async_playwright().start()
            self._browser = await self._launch()
        else:
            self._browser, self._owns_browser = browser, False
        self._context = await self._browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 860})
        self._context.set_default_timeout(8000)
        self.page = await self._context.new_page()
        self.page.on("response", self._on_response)
        self.page.on("download", lambda d: asyncio.ensure_future(self._on_download(d)))

    async def _launch(self) -> Browser:
        """Prefer Playwright's bundled Chromium; fall back to an installed Chrome/Edge.

        WORKER_BROWSER_CHANNEL (e.g. "chrome", "msedge") forces a specific browser.
        """
        forced = os.environ.get("WORKER_BROWSER_CHANNEL")
        channels = [forced] if forced else [None, "chrome", "msedge"]
        errors = []
        for channel in channels:
            try:
                return await self._pw.chromium.launch(headless=self.headless, channel=channel,
                                                      slow_mo=0 if self.headless else 150)
            except PlaywrightError as exc:
                errors.append(f"{channel or 'bundled chromium'}: {str(exc).splitlines()[0]}")
        raise RuntimeError("Could not launch a browser. Run `python -m playwright install chromium` or install "
                           "Chrome/Edge.\n" + "\n".join(errors))

    async def fork(self, downloads_dir: Path, screenshots_dir: Path) -> BrowserSession:
        """A fresh, isolated session (new cookies) on the same browser - used by the verifier."""
        other = BrowserSession(downloads_dir=downloads_dir, screenshots_dir=screenshots_dir, headless=self.headless)
        await other.start(browser=self._browser)
        return other

    async def close(self) -> None:
        try:
            if self._context:
                await self._context.close()
            if self._owns_browser:
                if self._browser:
                    await self._browser.close()
                if self._pw:
                    await self._pw.stop()
        except PlaywrightError:
            pass

    # ------------------------------------------------------------ events
    def _on_response(self, response) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.last_status = response.status
        except PlaywrightError:
            pass

    async def _on_download(self, download: Download) -> None:
        target = self.downloads_dir / download.suggested_filename
        await download.save_as(target)
        self.new_downloads.append(target)

    # ------------------------------------------------------------ perception
    async def snapshot(self) -> Snapshot:
        await self._settle()
        data = await self.page.evaluate(SNAPSHOT_JS)
        return Snapshot(url=data["url"], title=data["title"], text=data["text"],
                        http_status=self.last_status, refs=data["refs"])

    async def screenshot(self, label: str) -> Path:
        self._shot_counter += 1
        safe = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "step"
        path = self.screenshots_dir / f"{self._shot_counter:03d}-{safe}.jpg"
        await self.page.screenshot(path=str(path), type="jpeg", quality=60, full_page=True)
        return path

    async def _settle(self) -> None:
        with contextlib.suppress(PlaywrightTimeout):
            await self.page.wait_for_load_state("domcontentloaded", timeout=8000)

    # ------------------------------------------------------------ actions
    async def _locate(self, ref: int | str):
        loc = self.page.locator(f'[data-agent-ref="{int(ref)}"]')
        if await loc.count() == 0:
            raise BrowserActionError(
                f"Element [{ref}] is not on the current page (the page changed since that snapshot). "
                "Use refs from the latest snapshot.", kind="stale_ref")
        return loc.first

    async def describe(self, ref: int | str) -> dict:
        """Facts about an element used by the policy layer (label, kind, enclosing form)."""
        el = await self._locate(ref)
        return await el.evaluate("""(el) => {
            const txt = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
            const form = el.form || el.closest('form');
            let submit = '';
            if (form) { const b = form.querySelector('button[type=submit],button:not([type]),input[type=submit]');
                        if (b) submit = (b.innerText || b.value || '').trim(); }
            return { tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), text: txt,
                     href: el.getAttribute('href') || '', form_action: form ? (form.getAttribute('action') || '') : '',
                     form_method: form ? (form.getAttribute('method') || 'get').toLowerCase() : '',
                     form_submit_label: submit };
        }""")

    async def navigate(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except PlaywrightTimeout as exc:
            raise BrowserActionError(f"Navigation to {url} timed out.", kind="timeout", retryable=True) from exc
        except PlaywrightError as exc:
            msg = str(exc).splitlines()[0]
            if "ERR_CONNECTION_REFUSED" in msg or "ERR_CONNECTION_RESET" in msg:
                raise BrowserActionError(f"Could not connect to {url} ({msg}).", kind="network",
                                         retryable=True) from exc
            if "Download is starting" in msg:
                await asyncio.sleep(0.8)  # navigating straight to a file: the download handler saves it
                return
            raise BrowserActionError(f"Navigation failed: {msg}", kind="navigation") from exc

    async def click(self, ref: int | str) -> None:
        el = await self._locate(ref)
        try:
            await el.click(timeout=5000)
        except PlaywrightTimeout as exc:
            text = str(exc)
            if "intercepts pointer events" in text:
                before = text[:text.index("intercepts pointer events")]
                tags = re.findall(r"<\w+[^>]*>", before[-400:])
                hint = f" (blocked by {tags[-1][:100]})" if tags else ""
                raise BrowserActionError(
                    f"Click on [{ref}] was blocked because another element is covering it{hint}. "
                    "A modal dialog or overlay is probably open - find it in the snapshot and close/dismiss it first.",
                    kind="obscured") from exc
            raise BrowserActionError(f"Click on [{ref}] timed out; the element may be disabled or not clickable.",
                                     kind="timeout", retryable=True) from exc
        await self._after_action()

    async def type(self, ref: int | str, text: str, press_enter: bool = False) -> None:
        el = await self._locate(ref)
        try:
            await el.fill(text, timeout=5000)
            if press_enter:
                await el.press("Enter")
        except PlaywrightTimeout as exc:
            raise BrowserActionError(f"Could not type into [{ref}]; it may be obscured, disabled or not a text field.",
                                     kind="timeout") from exc
        except PlaywrightError as exc:
            raise BrowserActionError(f"Could not type into [{ref}]: {str(exc).splitlines()[0]}") from exc
        await self._after_action()

    async def select(self, ref: int | str, option: str) -> str:
        el = await self._locate(ref)
        options = await el.evaluate(
            "(el) => el.options ? [...el.options].map(o => ({v: o.value, t: o.text.trim()})) : null")
        if options is None:
            raise BrowserActionError(f"[{ref}] is not a dropdown.")
        wanted = option.strip().lower()
        match = (next((o for o in options if o["t"].lower() == wanted), None)
                 or next((o for o in options if o["v"].lower() == wanted), None)
                 or next((o for o in options if wanted and wanted in o["t"].lower()), None))
        if match is None:
            raise BrowserActionError(f"No option matching {option!r} in [{ref}]. Options: {[o['t'] for o in options]}")
        await el.select_option(value=match["v"])
        return match["t"]

    async def back(self) -> None:
        await self.page.go_back(wait_until="domcontentloaded")

    async def _after_action(self) -> None:
        # Give navigations / downloads triggered by the action a moment to start and settle.
        await asyncio.sleep(0.3)
        with contextlib.suppress(PlaywrightTimeout):
            await self.page.wait_for_load_state("domcontentloaded", timeout=8000)

    def take_new_downloads(self) -> list[Path]:
        files, self.new_downloads = self.new_downloads, []
        return files
