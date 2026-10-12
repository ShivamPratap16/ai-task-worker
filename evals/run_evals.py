"""Run the eval suite against a running sandbox.

    python -m sandbox &                                   # in another terminal
    python -m evals.run_evals                             # all tasks, realistic chaos
    python -m evals.run_evals --tasks latest_invoice --chaos hostile --repeat 3

Lessons are disabled so every task starts from the same knowledge.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.pricing import cost_usd, fmt_cost  # noqa: E402
from evals.tasks import TASKS  # noqa: E402
from worker.agent import Worker  # noqa: E402
from worker.config import Settings  # noqa: E402
from worker.events import RunRecorder  # noqa: E402
from worker.human import ScriptedHumanChannel  # noqa: E402
from worker.runner import new_event_loop  # noqa: E402

SANDBOX = "http://127.0.0.1:8001"


def _rel(path: Path) -> str:
    """Repo-relative when possible, so committed results don't leak local paths."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def run_one(task, chaos: str, seed: int, settings: Settings) -> dict:
    httpx.post(f"{SANDBOX}/__admin/reset", params={"chaos": chaos, "seed_value": seed}).raise_for_status()
    before = httpx.get(f"{SANDBOX}/__admin/state").json()
    run_id = f"eval-{task.id}-{time.strftime('%H%M%S')}"
    recorder = RunRecorder(run_id, settings.runs_dir / run_id)
    human = ScriptedHumanChannel(task.human)
    loop = new_event_loop()
    try:
        result = loop.run_until_complete(Worker(settings).run(task.prompt, recorder, human)).to_dict()
    finally:
        loop.close()
        recorder.close()
    after = httpx.get(f"{SANDBOX}/__admin/state").json()
    checks = task.check(before, after, result)
    passed = all(ok for _, ok in checks)
    v = result.get("verification")
    events = recorder.events
    # The provider may switch to a fallback model mid-run (free-tier quotas); record every model that served.
    models = list(dict.fromkeys([result["model"]] + [e.data["to"] for e in events if e.type == "model_switched"]))
    return {
        "task": task.id, "category": task.category, "passed": passed, "outcome": result["outcome"],
        "checks": [{"check": c, "ok": ok} for c, ok in checks],
        "verifier_said_achieved": v["achieved"] if v else None,
        "verifier_agrees": (v["achieved"] == passed) if v else None,
        "steps": result["steps"], "duration_s": result["duration_s"], "usage": result["usage"],
        "provider": result["provider"], "models": models,
        # Priced at the last model that served; exact unless a switch crossed a price boundary.
        "cost_usd": cost_usd(models[-1], result["usage"], result["provider"]),
        "faults_injected": after["faults_injected"],
        "auto_retries": sum(1 for e in events if e.type == "retry"),
        "failed_actions": sum(1 for e in events if e.type == "observation" and not e.data["ok"]),
        "human_interactions": [{"q": req.question, "a": resp.answer} for req, resp in human.log],
        "run_dir": _rel(recorder.run_dir),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", nargs="*", help="task ids (default: all)")
    ap.add_argument("--chaos", default="realistic", choices=["off", "realistic", "hostile"])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=ROOT / "evals" / "results", help="results directory")
    args = ap.parse_args()

    try:
        httpx.get(f"{SANDBOX}/__admin/health").raise_for_status()
    except httpx.HTTPError:
        print(f"Sandbox not reachable at {SANDBOX}. Start it with: python -m sandbox")
        return 2

    settings = Settings.from_env(use_lessons=False, headless=True)
    settings.extra["today"] = "2026-10-11"
    selected = [t for t in TASKS if not args.tasks or t.id in args.tasks]
    if not selected:
        print(f"No matching tasks. Available: {[t.id for t in TASKS]}")
        return 2
    rows = []
    for rep in range(args.repeat):
        for task in selected:
            print(f"[{rep + 1}/{args.repeat}] {task.id} ...", flush=True)
            row = run_one(task, args.chaos, args.seed + rep, settings)
            rows.append(row)
            fails = [c["check"] for c in row["checks"] if not c["ok"]]
            print(f"    {'PASS' if row['passed'] else 'FAIL'}  outcome={row['outcome']}  steps={row['steps']}  "
                  f"{row['duration_s']:.0f}s  {fmt_cost(row['models'][-1], row['cost_usd'])}  "
                  f"faults={len(row['faults_injected'])}  verifier_agrees={row['verifier_agrees']}"
                  + (f"  failed: {fails}" if fails else ""), flush=True)

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out_dir / f"{stamp}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")

    n = len(rows)
    passed = sum(r["passed"] for r in rows)
    judged = [r for r in rows if r["verifier_agrees"] is not None]
    lines = [f"# Eval results {stamp}", "",
             f"Model: {settings.model_label()} | chaos: {args.chaos} | seed: {args.seed} | runs: {n}", "",
             f"**Pass rate: {passed}/{n}**  |  verifier agreement with ground truth: "
             f"{sum(r['verifier_agrees'] for r in judged)}/{len(judged)}", "",
             "| task | category | result | outcome | model | steps | duration | faults injected | auto-retries "
             "| failed actions | tokens in/out (cached) | cost |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        u = r["usage"]
        lines.append(f"| {r['task']} | {r['category']} | {'PASS' if r['passed'] else 'FAIL'} | {r['outcome']} | "
                     f"{' -> '.join(r['models'])} | {r['steps']} | {r['duration_s']:.0f}s | "
                     f"{len(r['faults_injected'])} | {r['auto_retries']} | {r['failed_actions']} | "
                     f"{u['input_tokens']:,}/{u['output_tokens']:,} ({u.get('cache_read_tokens', 0):,}) | "
                     f"{fmt_cost(r['models'][-1], r['cost_usd'])} |")
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    total = f"${sum(costs):.2f}" + (f" ({n - len(costs)} runs unpriced)" if len(costs) < n else "") if costs else "n/a"
    lines += ["", f"Mean steps: {sum(r['steps'] for r in rows) / n:.1f}  |  mean duration: "
              f"{sum(r['duration_s'] for r in rows) / n:.0f}s  |  total cost: {total}  |  faults injected: "
              f"{sum(len(r['faults_injected']) for r in rows)}  |  auto-retries: {sum(r['auto_retries'] for r in rows)}",
              "", "Costs are approximate (list prices in evals/pricing.py; cache writes not counted)."]
    (out_dir / f"{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return 0 if passed == n else 1


if __name__ == "__main__":
    sys.exit(main())
