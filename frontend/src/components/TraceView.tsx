import type { ReactNode } from "react";
import { useNow } from "../hooks/useApp";
import { outcomeOf } from "../lib/constants";
import { cx } from "../lib/format";
import type { RunState, TimelineItem } from "../lib/runState";
import { Icon } from "./Icon";
import { Glyph, fmtOffset, fmtSpan, spanEnd, toolSteps, waitSpans } from "./inspectKit";
import "./inspect.css";

/** A "nice" axis step so the waterfall gets 3–5 labelled ticks. */
function tickStep(total: number) {
  const nice = [1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 900, 1800, 3600];
  return nice.find((s) => total / s <= 5) ?? 3600;
}
const tickLabel = (s: number) => (s >= 600 && s % 60 === 0 ? `${s / 60}m` : `${s}s`);

interface Row {
  key: string; depth: number; icon: ReactNode; label: string; sub?: string; ts: number; end: number;
  bar: "w" | "a" | "err" | "wait" | "root" | "grp"; running?: boolean; item?: TimelineItem; hook: boolean; strong?: boolean;
}

/** Latency waterfall of every tool step (LangSmith-style trace): where the time went, and what failed. */
export function TraceView({ run, onSelect }: { run: RunState; onSelect: (it: TimelineItem) => void }) {
  const live = !run.finished && !run.streamEnded;
  const now = useNow(live);
  const steps = toolSteps(run);
  if (!run.t0 || !steps.length) {
    return <div className="tx-empty"><Glyph name="layers" /><b>No spans yet</b><span>The trace appears once the worker takes its first action.</span></div>;
  }

  const t0 = run.t0;
  const lastTs = run.items.length ? run.items[run.items.length - 1].ts : t0;
  const tEnd = run.finished
    ? Math.max(t0 + (run.finished.duration_s ?? 0), ...steps.map((s) => spanEnd(s, lastTs)))
    : Math.max(now, ...steps.map((s) => spanEnd(s, now)));
  const total = Math.max(1, tEnd - t0);
  const pct = (t: number) => Math.min(100, Math.max(0, (100 * (t - t0)) / total));
  const endOf = (it: TimelineItem) => spanEnd(it, live ? now : tEnd);

  const worker = steps.filter((s) => s.who !== "verifier");
  const audit = steps.filter((s) => s.who === "verifier");
  const waits = waitSpans(run, now);
  const acting = worker.reduce((s, it) => s + Math.max(0, endOf(it) - it.ts), 0);
  const slowest = steps.reduce((a, b) => (endOf(b) - b.ts > endOf(a) - a.ts ? b : a));
  const failures = steps.filter((s) => s.failed).length;
  const ok = run.finished ? outcomeOf(run.finished.outcome).tone === "ok" : false;

  const stepRow = (it: TimelineItem, depth: number): Row => ({
    key: it.key, depth, ts: it.ts, end: endOf(it), item: it, hook: true,
    icon: it.failed ? <Glyph name="cx" className="tx-err" /> : <Icon name={it.icon} />,
    label: it.title, sub: it.text,
    bar: it.failed ? "err" : it.who === "verifier" ? "a" : "w", running: it.obs === "pending" && live,
  });
  const mid: Row[] = [
    ...worker.map((it) => stepRow(it, 1)),
    ...waits.map((w): Row => ({ key: w.key, depth: 1, ts: w.ts, end: w.endTs, icon: <Glyph name="hand" />, label: w.title, bar: "wait", running: w.open, hook: false })),
  ].sort((a, b) => a.ts - b.ts);
  const rows: Row[] = [
    { key: "root", depth: 0, ts: t0, end: tEnd, icon: <Glyph name="activity" />, label: "run", sub: run.task, bar: "root", running: live, hook: true, strong: true },
    ...mid,
  ];
  if (audit.length) {
    const aStart = audit[0].ts;
    const aEnd = Math.max(...audit.map(endOf), run.finished ? tEnd : now);
    rows.push({ key: "audit", depth: 1, ts: aStart, end: run.finished ? aEnd : now, icon: <Icon name="shield" />, label: "Independent audit", sub: "read-only auditor", bar: "a", running: live, hook: false, strong: true });
    rows.push(...audit.map((it) => stepRow(it, 2)));
  }

  const step = tickStep(total);
  const ticks: number[] = [];
  for (let s = 0; s <= total + 1e-6; s += step) ticks.push(s);
  const tickLines = ticks.map((s) => <i key={s} className="tk" style={{ left: `${(100 * s) / total}%` }} />);

  return (
    <div className="tx">
      <div className="tx-stats">
        <div><div className="k">Total</div><div className="v">{fmtSpan(total)}{live && <small>running</small>}</div></div>
        <div><div className="k">Tool steps</div><div className="v">{worker.length}{audit.length > 0 && <small>+{audit.length} audit</small>}</div></div>
        <div><div className="k">Time acting</div><div className="v">{Math.round((100 * acting) / total)}%<small>{fmtSpan(acting)}</small></div></div>
        <div><div className="k">Slowest step</div><div className="v">{fmtSpan(endOf(slowest) - slowest.ts)}<small>{slowest.title.toLowerCase()}</small></div></div>
        <div><div className="k">Failures</div>
          <div className="v"><span className={cx(failures > 0 && !ok && "tx-bad")}>{failures}</span>{failures > 0 && ok && <small>recovered</small>}</div></div>
      </div>

      <div className="tx-legend" aria-label="Legend">
        <span><i className="w" />Worker</span><span><i className="a" />Auditor</span><span><i className="wait" />Waiting on you</span><span><i className="err" />Failed</span>
      </div>

      <div className="tx-table">
        <div className="trace-grid" role="table" aria-label="Trace waterfall">
          <div className="tr tx-row tx-head" role="row">
            <div className="tx-name">Span</div>
            <div className="tx-num tx-start">Start</div>
            <div className="tx-num">Duration</div>
            <div className="tx-axis">
              {ticks.map((s) => <span key={s} style={{ left: `${(100 * s) / total}%` }}>{tickLabel(s)}</span>)}
            </div>
          </div>
          {rows.map((r) => (
            <div key={r.key} role="row"
              className={cx(r.hook ? "tr" : "tx-x", "tx-row", r.item && "click", r.bar === "err" && "is-err", r.strong && "strong")}
              onClick={r.item ? () => onSelect(r.item!) : undefined}
              title={r.item ? "Inspect this step" : undefined}>
              <div className="tx-name">
                {Array.from({ length: r.depth }, (_, j) => <span key={j} className="ind" />)}
                <span className="ic">{r.icon}</span>
                <span className="lbl">{r.label}</span>
                {r.sub && r.depth > 0 && <span className="sub">{r.sub}</span>}
              </div>
              <div className="tx-num tx-start">+{fmtOffset(r.ts - t0)}</div>
              <div className="tx-num">{r.running ? <span className="tx-live">{fmtSpan(r.end - r.ts)}</span> : fmtSpan(r.end - r.ts)}</div>
              <div className="tx-bar">
                {tickLines}
                <span className={cx("b", r.bar, r.running && "live")}
                  style={{ left: `${pct(r.ts)}%`, width: `max(2px, ${pct(r.end) - pct(r.ts)}%)` }} />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
