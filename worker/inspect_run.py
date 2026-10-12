"""Read a finished (or running) run's trace in the terminal: the debugging view.

    python -m worker inspect latest              # one line per event that matters
    python -m worker inspect latest --step 12    # everything about action #12, incl. the page the model saw
    python -m worker inspect latest --failures   # only what went wrong

It is a projection of runs/<id>/trace.jsonl (D12), like the dashboard and the report.
"""
from __future__ import annotations

import json
from pathlib import Path

MAX_LINE = 140
SERVER_ERROR = "WARNING: the server answered HTTP"   # written by tools._observe


def find_run(runs_dir: Path, ref: str) -> Path:
    """A run id, a path, or 'latest' (the most recently modified run)."""
    if ref == "latest":
        runs = [p for p in runs_dir.iterdir() if (p / "trace.jsonl").exists()]
        if not runs:
            raise SystemExit(f"No runs in {runs_dir}")
        return max(runs, key=lambda p: (p / "trace.jsonl").stat().st_mtime)
    for candidate in (Path(ref), runs_dir / ref):
        if (candidate / "trace.jsonl").exists():
            return candidate
    matches = [p for p in runs_dir.iterdir() if p.name.startswith(ref) and (p / "trace.jsonl").exists()]
    if len(matches) == 1:
        return matches[0]
    raise SystemExit(f"No unique run matching {ref!r} in {runs_dir}")


def load_events(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _short(text, n: int = MAX_LINE) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _args(args: dict) -> str:
    shown = {k: v for k, v in args.items() if k != "intent"}
    return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in shown.items())


def timeline(events: list[dict], failures_only: bool = False) -> list[str]:
    """One line per meaningful event. Actions are numbered so --step can expand them."""
    t0 = events[0]["ts"] if events else 0.0
    obs = {e["data"].get("call_id"): e["data"] for e in events if e["type"] == "observation"}
    lines, n = [], 0
    for e in events:
        d, kind = e["data"], e["type"]
        at = f"{e['ts'] - t0:7.1f}s"
        who = (d.get("actor") or "")[:1].upper() or " "   # W = worker, V = verifier
        line = None
        bad = False
        if kind == "action":
            n += 1
            o = obs.get(d.get("call_id"), {})
            content = o.get("content", "")
            server_error = SERVER_ERROR in content    # the click worked, the server did not
            bad = not o.get("ok", True) or server_error
            status = (f"ERR[{o.get('error_kind') or 'error'}] {_short(content, 80)}" if not o.get("ok", True)
                      else "ok, but " + _short(content[content.index(SERVER_ERROR):].splitlines()[0], 70)
                      if server_error else "ok")
            line = f"#{n:<3} {d['tool']}({_short(_args(d.get('args', {})), 60)})  {status}"
            if d.get("intent") and o.get("ok", True):
                line += f"  - {_short(d['intent'], 70)}"
        elif kind == "thought" and (d.get("text") or d.get("reasoning")):
            line = f"thinks: {_short(d.get('text') or d.get('reasoning'))}"
        elif kind == "plan":
            line = "plan: " + " | ".join(f"[{s['status']}] {s['step']}" for s in d.get("steps", []))
        elif kind in ("retry", "llm_retry"):
            bad = True
            line = f"{kind} #{d.get('attempt')}: {_short(d.get('error', ''), 100)}"
        elif kind == "policy_block":
            bad = True
            line = f"POLICY BLOCKED {d.get('tool')}: {_short(d.get('reason', ''))}"
        elif kind == "approval_requested":
            line = f"APPROVAL? {_short(d.get('action', ''))}  ({_short(d.get('reason', ''), 60)})"
        elif kind == "approval_resolved":
            bad = not d.get("approved")
            line = f"APPROVAL {'granted' if d.get('approved') else 'DENIED'} by {d.get('responder') or 'human'}" \
                   + (f": {_short(d['note'])}" if d.get("note") else "")
        elif kind == "question":
            line = f"ASKS HUMAN ({d.get('kind')}): {_short(d.get('question', ''))}"
        elif kind == "answer":
            line = f"human: {_short(d.get('answer') or ('approved' if d.get('approved') else '(no answer)'))}"
        elif kind == "operator_message":
            line = f"operator: {_short(d.get('text', ''))}"
        elif kind == "stall_detected":
            bad = True
            line = f"STALL detected: {_short(d.get('action', ''), 100)}"
        elif kind == "model_switched":
            line = f"model switched {d.get('from')} -> {d.get('to')}: {_short(d.get('reason', ''), 80)}"
        elif kind == "memory":
            line = f"remember {d.get('key')} = {_short(d.get('value'), 80)}"
        elif kind == "verification_started":
            line = "--- independent verification ---"
        elif kind == "verification":
            bad = not d.get("achieved")
            verdict = "inconclusive" if d.get("inconclusive") else ("ACHIEVED" if d.get("achieved") else "NOT achieved")
            line = f"VERDICT (round {d.get('round')}): {verdict}, confidence {d.get('confidence')}"
            line += "".join(f"\n{'':12}{'pass' if c.get('passed') else 'FAIL'}: {_short(c.get('check', ''), 110)}"
                            for c in d.get("checks", []))
            line += "".join(f"\n{'':12}issue: {_short(i, 110)}" for i in d.get("issues", []))
        elif kind == "error":
            bad = True
            line = f"ERROR in {d.get('where')}: {_short(d.get('message', ''))}"
        elif kind == "run_finished":
            line = f"=== {d.get('outcome')} after {d.get('steps')} steps, {d.get('duration_s')}s ==="
        if line is not None and (bad or not failures_only or kind == "run_finished"):
            lines.append(f"{at} {who} {line}")
    return lines


def step_detail(events: list[dict], number: int) -> str:
    actions = [e for e in events if e["type"] == "action"]
    if not 1 <= number <= len(actions):
        raise SystemExit(f"Action #{number} does not exist (this run has {len(actions)}).")
    a = actions[number - 1]["data"]
    o = next((e["data"] for e in events if e["type"] == "observation" and e["data"].get("call_id") == a["call_id"]), {})
    out = [f"Action #{number} by {a.get('actor')}: {a['tool']}",
           f"intent:  {a.get('intent') or '(none)'}",
           f"args:    {json.dumps(a.get('args', {}), ensure_ascii=False)}",
           f"result:  {'ok' if o.get('ok', True) else 'ERROR (' + (o.get('error_kind') or '?') + ')'}"]
    if o.get("url"):
        out.append(f"url:     {o['url']}")
    if o.get("screenshot"):
        out.append(f"screenshot: screenshots/{o['screenshot']}")
    out += ["", "What the model saw next:", "-" * 60, o.get("content", "(no observation recorded)")]
    return "\n".join(out)


def summary(run_dir: Path, events: list[dict]) -> list[str]:
    start = next((e["data"] for e in events if e["type"] == "run_started"), {})
    return [f"Run {run_dir.name}  ({start.get('provider')}/{start.get('model')})",
            f"Task: {start.get('task', '?')}", ""]
