// Small helpers shared by the inspect area (Inspector, TraceView, FlowView, StepDrawer): extra line icons that
// Icon.tsx doesn't carry, compact duration / clock formatting, and plan-step timing derived from the run.
import type { RunState, TimelineItem } from "../lib/runState";
import type { StepStatus } from "../lib/types";

const GLYPHS = {
  ccheck: '<circle cx="12" cy="12" r="9.5"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  cx: '<circle cx="12" cy="12" r="9.5"/><path d="m15 9-6 6M9 9l6 6"/>',
  cpend: '<circle cx="12" cy="12" r="9" stroke-dasharray="3 3"/>',
  cskip: '<circle cx="12" cy="12" r="9.5"/><path d="M8 12h8"/>',
  loader: '<circle cx="12" cy="12" r="9" opacity=".25"/><path d="M21 12a9 9 0 0 0-9-9"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  hand: '<path d="M18 11V6a2 2 0 0 0-4 0v1M14 10V4a2 2 0 0 0-4 0v2M10 10.5V6a2 2 0 0 0-4 0v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15"/>',
  chev: '<path d="m9 18 6-6-6-6"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  max: '<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>',
  minus: '<path d="M5 12h14"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  fit: '<path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/>',
  activity: '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7Z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/>',
} as const;

export type GlyphName = keyof typeof GLYPHS;

export function Glyph({ name, className }: { name: GlyphName; className?: string }) {
  return <svg className={className ? `i ${className}` : "i"} viewBox="0 0 24 24" aria-hidden="true" dangerouslySetInnerHTML={{ __html: GLYPHS[name] }} />;
}

/** Status glyph for a plan step / flow node. */
export function StateGlyph({ state, className }: { state: StepStatus | "stopped"; className?: string }) {
  if (state === "done") return <Glyph name="ccheck" className={className} />;
  if (state === "failed") return <Glyph name="cx" className={className} />;
  if (state === "in_progress") return <Glyph name="loader" className={cxs("rot", className)} />;
  if (state === "skipped" || state === "stopped") return <Glyph name="cskip" className={className} />;
  return <Glyph name="cpend" className={className} />;
}

const cxs = (...p: (string | undefined)[]) => p.filter(Boolean).join(" ");

/** Span duration: 0.8s · 9.4s · 34.1s · 1m 02s. */
export function fmtSpan(s: number): string {
  if (!Number.isFinite(s) || s < 0) s = 0;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
}

/** Offset from the run start as mm:ss. */
export function fmtOffset(s: number): string {
  const v = Math.max(0, Math.round(s));
  return `${String(Math.floor(v / 60)).padStart(2, "0")}:${String(v % 60).padStart(2, "0")}`;
}

/** Wall-clock time of an epoch-seconds timestamp, HH:MM:SS (with .mmm when asked). */
export function fmtClock(ts: number, ms = false): string {
  const d = new Date(ts * 1000);
  const p = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}${ms ? `.${p(d.getMilliseconds(), 3)}` : ""}`;
}

/** Tool steps (actions) in the order they started. */
export const toolSteps = (run: RunState) => run.items.filter((it) => it.key.startsWith("call-"));

export const spanEnd = (it: TimelineItem, now: number) => it.endTs ?? now;

/** Time the operator kept the worker waiting: approval / question asked → resolved (or now, while open). */
export interface WaitSpan { key: string; title: string; ts: number; endTs: number; open: boolean }
export function waitSpans(run: RunState, now: number): WaitSpan[] {
  const out: WaitSpan[] = [];
  let open: { key: string; title: string; ts: number } | null = null;
  for (const it of run.items) {
    const asks = it.title === "Approval requested" || it.title === "Worker asks" || it.title === "Plan sent for your review";
    const resolves = it.title === "Approved" || it.title === "Denied" || it.title.startsWith("Answer from");
    if (asks && !open) open = { key: `wait-${it.key}`, title: it.title === "Approval requested" ? "Wait for your approval" : it.title === "Worker asks" ? "Wait for your answer" : "Wait for plan review", ts: it.ts };
    else if (resolves && open) { out.push({ ...open, endTs: it.ts, open: false }); open = null; }
  }
  if (open) {
    const end = run.finished && run.t0 !== null ? Math.max(open.ts, run.t0 + (run.finished.duration_s ?? 0)) : now;
    out.push({ ...open, endTs: end, open: !run.finished });
  }
  return out;
}

// ---- plan-step timing ---------------------------------------------------------------------------------------
// The event log only carries the latest plan, so each step's start / end is observed as the plan changes and
// stamped with the run's event clock (the newest item's timestamp). Shared per run (keyed by its start time) so
// every view agrees, including views mounted after the steps happened.
interface StepTimes { start?: number; end?: number }
const timing = new Map<string, { sig: string; steps: Map<string, StepTimes> }>();

const runClock = (run: RunState) => (run.items.length ? run.items[run.items.length - 1].ts : run.t0 ?? 0);

function record(run: RunState) {
  if (!run.plan.length || run.t0 === null) return;
  const runId = String(run.t0);
  const sig = run.plan.map((s) => `${s.step}:${s.status}`).join("|");
  let rec = timing.get(runId);
  if (!rec) { rec = { sig: "", steps: new Map() }; timing.set(runId, rec); }
  if (rec.sig === sig) return;
  rec.sig = sig;
  const clock = runClock(run);
  let prevEnd = run.t0;
  for (const s of run.plan) {
    const t = rec.steps.get(s.step) ?? {};
    if (s.status !== "pending" && t.start === undefined) t.start = s.status === "in_progress" ? clock : prevEnd;
    if ((s.status === "done" || s.status === "failed" || s.status === "skipped") && t.end === undefined) t.end = clock;
    rec.steps.set(s.step, t);
    if (t.end !== undefined) prevEnd = t.end;
  }
}

export interface StepTiming { start?: number; end?: number; dur?: number; actions: number; retries: number; failures: number }

/** Start / end / duration of each plan step plus the tool activity inside its window (when it is known). */
export function planTiming(run: RunState): StepTiming[] {
  record(run);
  const rec = run.t0 !== null ? timing.get(String(run.t0)) : undefined;
  const steps = toolSteps(run).filter((it) => it.who !== "verifier");
  return run.plan.map((s) => {
    const t = rec?.steps.get(s.step) ?? {};
    const known = t.start !== undefined && t.end !== undefined && t.end > t.start;
    const inWin = (ts: number) => t.start !== undefined && ts >= t.start && (t.end === undefined || ts < t.end);
    const mine = t.start !== undefined && (known || t.end === undefined) ? steps.filter((it) => inWin(it.ts)) : [];
    const retries = t.start !== undefined && (known || t.end === undefined) ? run.items.filter((it) => it.title === "Automatic retry" && inWin(it.ts)).length : 0;
    return {
      start: t.start, end: t.end, dur: known ? t.end! - t.start! : undefined,
      actions: mine.length, retries, failures: mine.filter((it) => it.failed).length,
    };
  });
}
