"""CLI entry point.

    python -m worker run "Find the latest invoice from Globex ..."   # terminal mode
    python -m worker serve                                           # dashboard on :8000
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid

from worker.config import Settings


def _print_event(event) -> None:
    d, t = event.data, event.type
    actor = d.get("actor", "")
    tag = "  [verifier]" if actor == "verifier" else ""
    if t == "thought" and d.get("text"):
        print(f"{tag}  thinks: {d['text'][:300]}")
    elif t == "action":
        args = {k: v for k, v in d["args"].items() if k != "intent"}
        print(f"{tag}> {d['tool']} {args}  -- {d.get('intent', '')}")
    elif t == "observation" and not d["ok"]:
        print(f"{tag}  ! {d['error_kind']}: {d['content'].splitlines()[0][:200]}")
    elif t in ("retry", "policy_block", "stall_detected", "approval_requested", "approval_resolved",
               "question", "answer", "verification", "lesson", "error"):
        print(f"{tag}  [{t}] { {k: v for k, v in d.items() if k not in ('actor',)} }")


def cmd_run(args) -> int:
    from worker.agent import Worker
    from worker.events import RunRecorder
    from worker.human import ConsoleHumanChannel
    from worker.runner import new_event_loop

    settings = Settings.from_env(headless=not args.headed)
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    recorder = RunRecorder(run_id, settings.runs_dir / run_id)
    original_emit = recorder.emit

    def emit_and_print(type_, **data):
        event = original_emit(type_, **data)
        _print_event(event)
        return event
    recorder.emit = emit_and_print  # stream progress to the terminal

    loop = new_event_loop()
    result = loop.run_until_complete(Worker(settings).run(args.task, recorder, ConsoleHumanChannel()))
    recorder.close()
    print("\n" + (recorder.run_dir / "report.md").read_text(encoding="utf-8"))
    print(f"Artifacts: {recorder.run_dir}")
    return 0 if result.outcome == "completed_verified" else 1


def cmd_serve(args) -> int:
    import uvicorn
    uvicorn.run("worker.server:app", host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="worker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="run one task in the terminal")
    p_run.add_argument("task")
    p_run.add_argument("--headed", action="store_true", help="show the browser window")
    p_run.set_defaults(fn=cmd_run)
    p_serve = sub.add_parser("serve", help="start the dashboard")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(fn=cmd_serve)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
