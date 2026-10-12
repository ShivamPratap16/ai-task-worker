// Folds the run's event log into what the dashboard shows. The log is the source of truth: opening a run
// (or reconnecting) replays it from the start, so this reducer must be a pure function of the events.
import type { IconName } from "../components/Icon";
import { QUIET_TOOLS, toolMeta } from "./constants";
import type { Check, Finished, PlanStep, RunEvent, Usage } from "./types";

export type Category = "steps" | "reasoning" | "issues" | "verifier";

export interface Observation { ok: boolean; errorKind?: string; content: string; screenshot?: string | null }

export interface TimelineItem {
  key: string;
  tone: "" | "action" | "thought" | "reason" | "warn" | "error" | "good" | "ver" | "you";
  cats: Category[];
  icon: IconName;
  title: string;
  who?: "worker" | "verifier";
  ts: number;
  /** When the step's observation arrived (actions only): the trace waterfall uses ts..endTs. */
  endTs?: number;
  text?: string;
  note?: string;
  reasoning?: string;
  args?: Record<string, unknown>;
  /** For actions: "pending" until the observation arrives; "stopped" if the run ended first. */
  obs?: Observation | "pending" | "stopped";
  checks?: Check[];
  issues?: string[];
  failed?: boolean;
}

export interface Pending {
  kind: string;
  question: string;
  reason?: string;
  options?: string[];
  steps?: PlanStep[];
  screenshot?: string | null;
}

export interface Shot { name: string; url?: string | null }

export interface RunState {
  task: string;
  model: string;
  t0: number | null;
  items: TimelineItem[];
  plan: PlanStep[];
  facts: Record<string, string>;
  freshFact?: string;
  pending: Record<string, Pending>;
  shots: Shot[];
  usage: Usage | null;
  steps: number;
  finished: Finished | null;
  streamEnded: boolean;
}

export type RunAction =
  | { type: "reset"; task: string }
  | { type: "event"; event: RunEvent }
  | { type: "end" };

export const initialRun = (task = ""): RunState => ({
  task, model: "", t0: null, items: [], plan: [], facts: {}, pending: {}, shots: [], usage: null, steps: 0,
  finished: null, streamEnded: false,
});

const without = <T>(rec: Record<string, T>, key?: string) => {
  const next = { ...rec };
  if (key) delete next[key];
  return next;
};

export function runReducer(state: RunState, action: RunAction): RunState {
  if (action.type === "reset") return initialRun(action.task);
  if (action.type === "end") return { ...state, streamEnded: true };

  const { type, ts, data: d } = action.event;
  const who = d.actor === "verifier" ? "verifier" : d.actor ? "worker" : undefined;
  const vcat: Category[] = who === "verifier" ? ["verifier"] : [];
  const add = (...items: Omit<TimelineItem, "key">[]) =>
    ({ ...state, items: [...state.items, ...items.map((it, i) => ({ ...it, key: `${action.event.seq}-${i}` }))] });

  switch (type) {
    case "run_started":
      return { ...state, task: d.task ?? state.task, model: `${d.provider}/${d.model}`, t0: ts };

    case "thought": {
      const items: Omit<TimelineItem, "key">[] = [];
      if (d.reasoning) items.push({ tone: "reason", cats: ["reasoning", ...vcat], icon: "brain", title: "Reasoning", who, ts, reasoning: d.reasoning });
      if (d.text) items.push({ tone: "thought", cats: ["reasoning", ...vcat], icon: "sparkle", title: who === "verifier" ? "Auditor" : "Worker", who, ts, text: d.text });
      return items.length ? add(...items) : state;
    }

    case "action": {
      if (QUIET_TOOLS.has(d.tool ?? "")) return state;
      const [icon, title] = toolMeta(d.tool ?? "");
      const next = { ...state, steps: state.steps + 1 };
      return {
        ...next,
        items: [...state.items, {
          key: `call-${d.call_id ?? action.event.seq}`, tone: "action", cats: ["steps", ...vcat], icon, title, who, ts,
          text: d.intent || undefined, args: d.args, obs: "pending",
        }],
      };
    }

    case "observation": {
      if (QUIET_TOOLS.has(d.tool ?? "")) return state;
      const shots = d.screenshot ? [...state.shots, { name: d.screenshot, url: d.url }] : state.shots;
      const obs: Observation = { ok: !!d.ok, errorKind: d.error_kind, content: d.content ?? "", screenshot: d.screenshot };
      const key = `call-${d.call_id}`;
      const idx = d.call_id ? state.items.findIndex((it) => it.key === key) : -1;
      if (idx >= 0) {
        const items = state.items.slice();
        const it = items[idx];
        items[idx] = d.ok ? { ...it, obs, endTs: ts } : { ...it, obs, endTs: ts, tone: "error", failed: true, cats: [...it.cats, "issues"] };
        return { ...state, items, shots };
      }
      return { ...add({ tone: d.ok ? "" : "error", cats: d.ok ? ["steps", ...vcat] : ["issues", ...vcat], icon: "eye", title: "Observation", who, ts, obs }), shots };
    }

    case "retry":
      return add({ tone: "warn", cats: ["issues"], icon: "retry", title: "Automatic retry", who, ts,
        text: `${toolMeta(d.tool ?? "")[1]} attempt ${d.attempt} failed — retrying in ${d.delay_s}s`, note: d.error });
    case "policy_block":
      return add({ tone: "warn", cats: ["issues"], icon: "shield", title: "Blocked by policy", who, ts, text: `${toolMeta(d.tool ?? "")[1]}: ${d.reason}` });
    case "stall_detected":
      return add({ tone: "warn", cats: ["issues"], icon: "retry", title: "Loop guard", ts, text: "Stall detected — the worker was told to change approach." });

    case "approval_requested": {
      const pending = { ...state.pending, [d.request_id!]: { kind: "approval", question: d.action ?? "", reason: d.reason, screenshot: d.screenshot } };
      return { ...add({ tone: "warn", cats: ["issues"], icon: "shield", title: "Approval requested", who, ts, text: d.action, note: d.reason }), pending };
    }
    case "approval_resolved":
      return { ...add({ tone: d.approved ? "good" : "error", cats: ["issues"], icon: d.approved ? "check" : "x", title: d.approved ? "Approved" : "Denied", ts,
        note: `by ${d.responder}${d.note ? ` — ${d.note}` : ""}` }), pending: without(state.pending, d.request_id) };

    case "question": {
      const pending = { ...state.pending, [d.request_id!]: { kind: d.kind ?? "clarification", question: d.question ?? "", options: d.options, steps: d.steps, screenshot: d.screenshot } };
      const item: Omit<TimelineItem, "key"> = d.kind === "plan_review"
        ? { tone: "warn", cats: ["issues"], icon: "list", title: "Plan sent for your review", who, ts, text: `${(d.steps ?? []).length} steps — waiting for your approval before acting.` }
        : { tone: "warn", cats: ["issues"], icon: "msg", title: "Worker asks", who, ts, text: d.question };
      return { ...add(item), pending };
    }
    case "answer":
      return { ...add({ tone: "good", cats: ["issues"], icon: "msg", title: `Answer from ${d.responder || "operator"}`, ts, text: d.answer }), pending: without(state.pending, d.request_id) };

    case "operator_message":
      return add({ tone: "you", cats: ["issues"], icon: "send", title: "You", ts, text: d.text, note: "Delivered with the worker's next step." });
    case "model_switched":
      return { ...add({ tone: "warn", cats: ["issues"], icon: "cpu", title: `Switched model to ${d.to}`, ts, text: `${d.reason}.`,
        note: "The run continues on the backup model with its history intact." }), model: `${state.model.split("/")[0]}/${d.to}` };

    case "plan": return { ...state, plan: d.steps ?? [] };
    case "memory": return { ...state, facts: { ...state.facts, [d.key!]: String(d.value ?? "") }, freshFact: d.key };
    case "lesson": return add({ tone: "good", cats: ["reasoning"], icon: "book", title: "Lesson saved", who, ts, text: d.lesson });

    case "verification_started":
      return add({ tone: "ver", cats: ["verifier"], icon: "shield", title: "Independent audit started", who: "verifier", ts,
        text: "A separate auditor is checking the live system in a fresh, read-only browser session." });
    case "verification":
      return add({ tone: d.achieved ? "good" : "error", cats: d.achieved ? ["verifier"] : ["verifier", "issues"], icon: "shield",
        title: `Verification round ${d.round}: ${d.achieved ? "passed" : "failed"}`, who: "verifier", ts,
        note: `${d.confidence} confidence`, checks: d.checks, issues: d.issues });

    case "usage":
      return { ...state, usage: { input_tokens: d.input_tokens ?? 0, output_tokens: d.output_tokens ?? 0, cache_read_tokens: d.cache_read_tokens ?? 0 } };
    case "error":
      return add({ tone: "error", cats: ["issues"], icon: "alert", title: `Error · ${d.where ?? ""}`, ts, text: d.message });

    case "run_finished":
      return {
        ...state,
        finished: d as Finished,
        pending: {},
        items: state.items.map((it) => (it.obs === "pending" ? { ...it, obs: "stopped", endTs: ts } : it)),
      };
    default:
      return state;
  }
}
