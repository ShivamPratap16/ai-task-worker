"""Daily request accounting per model, for free tiers with small per-model daily quotas.

The ledger counts successful requests per model per day and remembers what the
provider told us when a quota ran out (the daily limit and when it resets). The
provider uses it to skip models that are known to be exhausted; the dashboard
shows it so the operator knows what is left before starting a task.

It is persisted next to the runs (runs/quota.json) so restarts do not forget.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date
from pathlib import Path
import contextlib


def parse_quota_error(text: str) -> dict:
    """Pull the useful facts out of a provider's 429 body (Gemini puts them in free text and details)."""
    out: dict = {}
    if m := re.search(r"limit:\s*(\d+)", text):
        out["limit"] = int(m.group(1))
    if m := re.search(r"model:\s*([\w.\-]+)", text):
        out["quota_model"] = m.group(1)
    if m := re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", text):
        out["retry_s"] = float(m.group(1))
    elif m := re.search(r"retry in ((?:\d+h)?(?:\d+m)?(?:\d+(?:\.\d+)?s)?)", text):
        units = {"h": 3600, "m": 60, "s": 1}
        out["retry_s"] = sum(float(n) * units[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)([hms])", m.group(1)))
    return out


def human_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{max(minutes, 1)}m"


class QuotaLedger:
    _shared: dict[Path, QuotaLedger] = {}
    _registry_lock = threading.Lock()

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict = {"day": date.today().isoformat(), "models": {}}
        with contextlib.suppress(OSError, ValueError):
            self.data = json.loads(path.read_text(encoding="utf-8"))

    @classmethod
    def shared(cls, path: Path) -> QuotaLedger:
        path = path.resolve()
        with cls._registry_lock:
            if path not in cls._shared:
                cls._shared[path] = cls(path)
            return cls._shared[path]

    def _model(self, model: str) -> dict:
        today = date.today().isoformat()
        if self.data.get("day") != today:  # new day: counts restart; learned limits and resets are kept
            self.data["day"] = today
            for m in self.data["models"].values():
                m["requests"] = 0
        return self.data["models"].setdefault(model, {"requests": 0})

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass  # accounting must never break a run

    def record(self, model: str) -> None:
        with self.lock:
            self._model(model)["requests"] += 1
            self._save()

    def mark_exhausted(self, model: str, limit: int | None, retry_s: float | None) -> None:
        with self.lock:
            m = self._model(model)
            if limit:
                m["daily_limit"] = limit
            # Without a reset time from the provider, assume a few hours rather than forever.
            m["exhausted_until"] = time.time() + (retry_s if retry_s else 6 * 3600)
            self._save()

    def exhausted(self, model: str) -> bool:
        with self.lock:
            return self._model(model).get("exhausted_until", 0) > time.time()

    def status(self, model: str) -> dict:
        with self.lock:
            m = dict(self._model(model))
        until = m.pop("exhausted_until", 0)
        m["exhausted"] = until > time.time()
        if m["exhausted"]:
            m["resets_in"] = human_duration(until - time.time())
        return m
