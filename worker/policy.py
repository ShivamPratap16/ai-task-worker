"""Action policy: what the agent may do on its own, what needs a human, what is forbidden.

Enforced in the tool executor, *not* in the prompt. A model can be talked
into anything; this layer cannot. Decisions are made on observable facts about
the target element and the destination URL. A submit is judged on two signals:
its label ("Submit for payment") and the path its form posts to (/vendors/1/bank).
Either one is enough to require approval, so a harmless-looking "Save" button on
a bank-details form is still caught, and so is a scary label on a harmless form.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

# Labels of actions with real-world, hard-to-reverse consequences.
RISKY_LABEL = re.compile(
    r"\b(submit for payment|pay|payment|approve|delete|remove|transfer|wire|"
    r"update bank|bank details|send|send email|publish|cancel order)\b", re.I)
# Actions that only read, navigate or authenticate - allowed even in read-only mode.
SAFE_LABEL = re.compile(
    r"^\s*(search|filter|go|find|sign in|log in|login|dismiss|close|ok|next|previous|back)\s*$", re.I)
# Form endpoints (URL path segments) whose POST has real-world consequences.
RISKY_ENDPOINT = re.compile(r"/(submit|pay|payments?|transfer|bank|delete|remove|approve|reply|send)(/|$)", re.I)
BLOCKED_PATH = re.compile(r"^/__admin", re.I)


@dataclass
class Decision:
    allowed: bool
    needs_approval: bool = False
    reason: str = ""


def check_url(url: str, allowed_origins: tuple[str, ...], base: str | None = None) -> Decision:
    absolute = urljoin(base or "", url)
    parsed = urlparse(absolute)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.scheme in ("javascript", "data", "file"):
        return Decision(False, reason=f"{parsed.scheme}: URLs are not allowed")
    if origin not in allowed_origins:
        return Decision(False, reason=f"{origin} is outside the systems this worker is authorised to use "
                                      f"({', '.join(allowed_origins)})")
    if BLOCKED_PATH.match(parsed.path):
        return Decision(False, reason="administrative endpoints are off-limits")
    return Decision(True)


def risky_endpoint(element: dict) -> bool:
    """True if the element's form *posts* to a risky path. GET forms only read."""
    mutating = element.get("form_method", "post") != "get"
    return mutating and bool(RISKY_ENDPOINT.search(urlparse(element.get("form_action") or "").path))


def assess_interaction(element: dict, *, submits: bool, mode: str, read_only: bool) -> Decision:
    """Decide on a button click or an Enter keypress in a form (submits=True).

    Plain links are navigation (GET) and are governed by check_url only - their
    text ("URGENT: update bank details") says nothing about the action's impact.
    """
    if not submits:
        return Decision(True)
    label = element.get("form_submit_label") if element.get("tag") in ("input", "textarea", "select") \
        and element.get("type") not in ("submit", "button") else element.get("text", "")
    label = (label or "").strip()
    mutating = element.get("form_method", "post") != "get"
    endpoint = urlparse(element.get("form_action") or "").path
    risky_ep = risky_endpoint(element)

    if read_only:
        if risky_ep:
            return Decision(False, reason=f"read-only mode: '{label}' posts to {endpoint}, which changes data")
        if not mutating or SAFE_LABEL.match(label):
            return Decision(True)
        return Decision(False, reason=f"read-only mode: '{label}' would change data")

    if mode == "never":
        return Decision(True)
    if RISKY_LABEL.search(label):
        return Decision(True, needs_approval=True, reason=f"label '{label}' marks a high-impact action")
    if risky_ep:
        return Decision(True, needs_approval=True, reason=f"'{label}' posts to {endpoint}, a high-impact endpoint")
    if mode == "always" and mutating and not SAFE_LABEL.match(label):
        return Decision(True, needs_approval=True, reason=f"'{label}' changes data (approval mode: always)")
    return Decision(True)
