"""CLI entry point.

    python -m worker run "Find the latest invoice from Globex ..."   # terminal mode
    python -m worker serve                                           # dashboard on :8000
    python -m worker check                                           # test the LLM key (1 request)
    python -m worker replay cassettes/<run>.json [--headed]          # re-enact a recorded run, no API key
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
    from worker.human import ConsoleHumanChannel
    return _run_in_terminal(Settings.from_env(headless=not args.headed), args.task, ConsoleHumanChannel())


def _run_in_terminal(settings, task, human, provider=None) -> int:
    from worker.agent import Worker
    from worker.events import RunRecorder
    from worker.runner import new_event_loop

    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    recorder = RunRecorder(run_id, settings.runs_dir / run_id)
    original_emit = recorder.emit

    def emit_and_print(type_, **data):
        event = original_emit(type_, **data)
        _print_event(event)
        return event
    recorder.emit = emit_and_print  # stream progress to the terminal

    loop = new_event_loop()
    result = loop.run_until_complete(Worker(settings, provider).run(task, recorder, human))
    recorder.close()
    print("\n" + (recorder.run_dir / "report.md").read_text(encoding="utf-8"))
    print(f"Artifacts: {recorder.run_dir}")
    return 0 if result.outcome == "completed_verified" else 1


def cmd_replay(args) -> int:
    """Re-enact a recorded run: reset the sandbox to the recorded chaos/seed, then feed the recorded
    model turns (and human answers) to the real loop, browser and sandbox."""
    import httpx
    from worker.llm import make_provider
    from worker.llm.recording import ReplayHumanChannel

    settings = Settings.from_env(headless=not args.headed, provider="replay", cassette=args.cassette, record="")
    provider = make_provider(settings)  # also restores the recorded "today"
    c = provider.cassette
    sandbox = c.get("sandbox") or {}
    origin = "/".join(settings.start_url.split("/")[:3])
    print(f"Replaying {c['provider']}/{c['model']} run {c['run_id']} (recorded {c['created']}, today={c['today']})")
    print(f"Task: {c['task']}")
    if sandbox.get("chaos"):
        seed = sandbox.get("seed") if sandbox.get("seed") is not None else 7
        if sandbox.get("seed") is None:
            print("  note: the cassette has no sandbox seed; assuming the default (7)")
        try:
            httpx.post(f"{origin}/__admin/reset", params={"chaos": sandbox["chaos"], "seed_value": seed},
                       timeout=5).raise_for_status()
            print(f"Sandbox reset: chaos={sandbox['chaos']} seed={seed}")
        except httpx.HTTPError as exc:
            print(f"Could not reset the sandbox at {origin} ({exc}). Is it running? (python -m sandbox)")
            return 1
        from worker.llm.recording import sandbox_info
        if sandbox.get("state_sha") and sandbox_info(settings.start_url)["state_sha"] != sandbox["state_sha"]:
            print("  WARNING: the sandbox's start state differs from the recording; the replay may diverge.")
    else:
        print("  note: the cassette does not say how the sandbox was set up; using it as it is")
    code = _run_in_terminal(settings, c["task"], ReplayHumanChannel(c.get("human", [])), provider)
    if provider.divergences:
        print(f"Replay fidelity: the model's input differed from the recording at {len(provider.divergences)} "
              f"turn(s), first at {provider.divergences[0]}.")
    else:
        print("Replay fidelity: every turn saw exactly what the recorded model saw.")
    return code


def cmd_check(args) -> int:
    """One cheap request: confirms the key, the model name and tool calling all work."""
    from worker.llm import LLMError, ToolSpec, make_provider
    from worker.runner import new_event_loop

    settings = Settings.from_env()
    print(f"Provider: {settings.model_label()}  (rate limit: {settings.resolved_rpm() or 'none'} req/min)")
    provider = make_provider(settings)
    if provider.name == "replay":  # nothing to probe: the "model" is a file
        c = provider.cassette
        turns = sum(len(cv["turns"]) for cv in c["conversations"])
        print(f"OK: cassette of a {c['provider']}/{c['model']} run, {len(c['conversations'])} conversation(s), "
              f"{turns} turns. Task: {c['task']}")
        return 0
    if settings.record:
        print(f"Recording cassettes to: {settings.record}")
    convo = provider.conversation("You are a connectivity test.", [ToolSpec(
        "ping", "Echo a message.", {"type": "object", "properties": {"message": {"type": "string"}},
                                    "required": ["message"], "additionalProperties": False})])
    convo.add_user_text("Call the ping tool with message 'ok'. Do not reply with text.")
    async def probe():
        try:
            return await convo.complete()
        finally:
            await provider.aclose()
    loop = new_event_loop()
    try:
        turn = loop.run_until_complete(probe())
    except LLMError as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        loop.close()
    if turn.tool_calls and turn.tool_calls[0].name == "ping":
        print(f"OK: tool calling works ({turn.model}; {turn.usage.input_tokens} in / {turn.usage.output_tokens} out tokens)")
        return 0
    print(f"WARNING: the model answered without calling the tool: {turn.text[:200]!r}. "
          "It may be too weak for this agent; try another model.")
    return 1


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
    p_check = sub.add_parser("check", help="verify the LLM key/model with one tiny request")
    p_check.set_defaults(fn=cmd_check)
    p_replay = sub.add_parser("replay", help="re-enact a recorded run (cassette) without an API key")
    p_replay.add_argument("cassette")
    p_replay.add_argument("--headed", action="store_true", help="show the browser window")
    p_replay.set_defaults(fn=cmd_replay)
    p_serve = sub.add_parser("serve", help="start the dashboard")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(fn=cmd_serve)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
