import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { Outcome } from "../lib/types";
import { outcomeOf } from "../lib/constants";
import { cx } from "../lib/format";
import type { RunState } from "../lib/runState";
import { Icon, type IconName } from "./Icon";
import { Glyph, fmtClock, fmtSpan, planTiming, type GlyphName } from "./inspectKit";
import "./inspect.css";

type NodeState = "done" | "running" | "failed" | "pending" | "skipped";

const STATE_GLYPH: Record<NodeState, GlyphName> = { done: "ccheck", running: "loader", failed: "cx", pending: "cpend", skipped: "cskip" };
const SHORT: Record<Outcome, string> = {
  completed_verified: "Verified", completed_unverified: "Not verified", blocked: "Blocked", failed: "Failed", error: "Error", cancelled: "Cancelled",
};

// Canvas geometry (px, before zoom). Mirrors the concept: trigger → plan group → audit → result.
const W = 800, NODE_H = 52, PITCH = 64, G_TOP_PAD = 24, G_BOT_PAD = 16;
const X = { trigger: 4, triggerW: 144, group: 160, groupW: 272, step: 172, stepW: 248, audit: 452, auditW: 196, result: 668, resultW: 128 };

type Pt = [number, number];
interface Rect { left: number; top: number; width: number }

/** Bezier between two ports: horizontal tangents across the canvas, vertical ones when stacked. */
function curve([[x1, y1], [x2, y2]]: [Pt, Pt], mode: "h" | "v") {
  if (mode === "h") { const mx = (x1 + x2) / 2; return `M${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`; }
  const my = (y1 + y2) / 2;
  return `M${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2}`;
}

/** Node positions. "h": trigger → plan group → audit → result, left to right (concept B); "v": stacked for phones. */
function layout(mode: "h" | "v", n: number, nChecks: number) {
  const groupH = G_TOP_PAD + n * PITCH - (PITCH - NODE_H) + G_BOT_PAD;
  const auditH = nChecks ? NODE_H + 8 + Math.min(4, nChecks) * 23 : NODE_H;
  if (mode === "h") {
    const H = Math.max(360, groupH + 56, auditH + 56);
    const cy = Math.round(H / 2);
    const gTop = Math.round((H - groupH) / 2);
    const stepTop = (i: number) => gTop + G_TOP_PAD + i * PITCH;
    const mid = (i: number) => stepTop(i) + NODE_H / 2;
    return {
      mode, W, H, stepTop, stepCx: X.step + X.stepW / 2,
      trigger: { left: X.trigger, top: cy - NODE_H / 2, width: X.triggerW } as Rect,
      group: { left: X.group, top: gTop, width: X.groupW, height: groupH },
      step: (i: number): Rect => ({ left: X.step, top: stepTop(i), width: X.stepW }),
      audit: { left: X.audit, top: cy - Math.round(auditH / 2), width: X.auditW } as Rect,
      result: { left: X.result, top: cy - NODE_H / 2, width: X.resultW } as Rect,
      w0: [[X.trigger + X.triggerW, cy], [X.step, mid(0)]] as [Pt, Pt],
      w1: [[X.step + X.stepW, mid(n - 1)], [X.audit, cy]] as [Pt, Pt],
      w2: [[X.audit + X.auditW, cy], [X.result, cy]] as [Pt, Pt],
    };
  }
  const VW = 320, cx = VW / 2, GAP = 32;
  const gTop = 8 + NODE_H + GAP;
  const stepTop = (i: number) => gTop + G_TOP_PAD + i * PITCH;
  const auditTop = gTop + groupH + GAP;
  const resultTop = auditTop + auditH + GAP;
  return {
    mode, W: VW, H: resultTop + NODE_H + 8, stepTop, stepCx: cx,
    trigger: { left: cx - 100, top: 8, width: 200 } as Rect,
    group: { left: 8, top: gTop, width: VW - 16, height: groupH },
    step: (i: number): Rect => ({ left: 20, top: stepTop(i), width: VW - 40 }),
    audit: { left: 20, top: auditTop, width: VW - 40 } as Rect,
    result: { left: cx - 80, top: resultTop, width: 160 } as Rect,
    w0: [[cx, 8 + NODE_H], [cx, stepTop(0)]] as [Pt, Pt],
    w1: [[cx, stepTop(n - 1) + NODE_H], [cx, auditTop]] as [Pt, Pt],
    w2: [[cx, auditTop + auditH], [cx, resultTop]] as [Pt, Pt],
  };
}

/** Plan steps get an icon that hints at what they do. */
function stepIcon(text: string): IconName {
  const t = text.toLowerCase();
  if (/mail|inbox|email/.test(t)) return "mail";
  if (/pdf|document|read|extract/.test(t)) return "file";
  if (/sign in|log ?in|login|credential/.test(t)) return "key";
  if (/create|record|enter|fill|update|save/.test(t)) return "db";
  if (/confirm|report|finish|tell/.test(t)) return "flag";
  if (/search|find|look/.test(t)) return "eye";
  if (/open|navigate|go to|visit/.test(t)) return "globe";
  if (/pay|bank|amount/.test(t)) return "bank";
  return "list";
}

function Node({ state, style, icon, title, sub, subClass, tag, children, tall }: {
  state: NodeState; style: CSSProperties; icon: ReactNode; title: string; sub?: ReactNode; subClass?: string; tag?: ReactNode; children?: ReactNode; tall?: boolean;
}) {
  return (
    <div className={cx("fnode", `n-${state}`, tall && "tall")} style={style}>
      <span className="ico">{icon}</span>
      <div><b title={title}>{title}</b>{sub !== undefined && <span className={cx("s", subClass)}>{sub}</span>}{children}</div>
      {tag}
      <span className={cx("stt", `n-${state}`)} title={state}><Glyph name={STATE_GLYPH[state]} className={state === "running" ? "rot" : undefined} /></span>
    </div>
  );
}

/** The run as a workflow (n8n-style canvas): trigger → plan steps → independent audit → result. */
export function FlowView({ run }: { run: RunState }) {
  const f = run.finished;
  const running = !f && !run.streamEnded;
  const auditItems = run.items.filter((it) => it.who === "verifier");
  const verdict = [...run.items].reverse().find((it) => it.title.startsWith("Verification round"));
  const auditState: NodeState = verdict ? (verdict.tone === "good" ? "done" : "failed")
    : auditItems.length ? (running ? "running" : "failed") : f ? "skipped" : "pending";
  const o = f ? outcomeOf(f.outcome) : null;
  const resultState: NodeState = !o ? "pending" : o.tone === "ok" ? "done" : o.tone === "err" ? "failed" : "skipped";
  const times = planTiming(run);

  const steps = run.plan.map((s, i) => {
    const state: NodeState = s.status === "in_progress" ? (running ? "running" : "skipped")
      : s.status === "done" ? "done" : s.status === "failed" ? "failed" : s.status === "skipped" ? "skipped" : "pending";
    const t = times[i];
    const acts = t ? `${t.actions} action${t.actions === 1 ? "" : "s"}` : "";
    const sub = state === "running" ? "running"
      : t?.dur !== undefined ? `${acts} · ${fmtSpan(t.dur)}`
      : state === "pending" ? "pending" : state === "skipped" ? (s.status === "in_progress" ? "stopped" : "skipped") : state === "done" ? "done" : "failed";
    return { text: s.step, state, sub, retries: t?.retries ?? 0, failures: t?.failures ?? 0 };
  });

  const checks = verdict?.checks ?? [];
  const aDur = auditItems.length ? Math.max(...auditItems.map((it) => it.endTs ?? it.ts)) - auditItems[0].ts : 0;
  const triggerState: NodeState = "done";
  const firstState: NodeState = steps[0]?.state ?? "pending";
  const lastState: NodeState = steps.length ? steps[steps.length - 1].state : "pending";
  const wire = (from: NodeState, to: NodeState) => cx(from === "done" && "done", to === "running" && "live");

  // Fit to the canvas width until the operator zooms by hand; narrow canvases stack the flow vertically.
  const box = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState<number | null>(null);
  const [width, setWidth] = useState(W + 16);
  useLayoutEffect(() => {
    const el = box.current; if (!el) return;
    const measure = () => setWidth(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure); ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const g = layout(width < 600 ? "v" : "h", Math.max(1, steps.length), checks.length);
  const z = zoom ?? Math.min(1, (width - 16) / g.W);
  const step = (d: number) => setZoom(Math.min(1.5, Math.max(0.4, Math.round((z + d) * 10) / 10)));
  const ports = [...g.w0, ...g.w1, ...g.w2];

  return (
    <div className={cx("fl", g.mode === "v" && "fl-v")} ref={box}>
      <div className="fl-scroll" style={{ height: g.H * z + 56 }}>
        <div className="fl-size" style={{ width: g.W * z, height: g.H * z }}>
          <div className="fl-inner" style={{ width: g.W, height: g.H, transform: `scale(${z})` }}>
            <svg className="fl-wires" viewBox={`0 0 ${g.W} ${g.H}`} aria-hidden="true">
              <path className={wire(triggerState, firstState)} d={curve(g.w0, g.mode)} />
              {steps.slice(1).map((s, i) => (
                <path key={i} className={wire(steps[i].state, s.state)} d={`M${g.stepCx} ${g.stepTop(i) + NODE_H} V ${g.stepTop(i + 1)}`} />
              ))}
              <path className={wire(lastState, auditState)} d={curve(g.w1, g.mode)} />
              <path className={wire(auditState, resultState)} d={curve(g.w2, g.mode)} />
              {ports.map(([x, y], i) => <circle key={i} cx={x} cy={y} r="3" />)}
            </svg>

            <Node state={triggerState} style={g.trigger} icon={<Glyph name="inbox" />} title="Task received"
              sub={run.t0 !== null ? fmtClock(run.t0) : "from you"} />

            <div className="fl-group" style={g.group}>
              <span className="gh">Plan · {steps.length ? `${steps.length} step${steps.length === 1 ? "" : "s"}` : "not written yet"} · worker</span>
            </div>
            {steps.length ? steps.map((s, i) => (
              <Node key={i} state={s.state} style={g.step(i)} icon={<Icon name={stepIcon(s.text)} />} title={s.text} sub={s.sub}
                tag={s.retries > 0 ? <span className="tag"><Icon name="retry" />{s.retries} retr{s.retries === 1 ? "y" : "ies"}</span>
                  : s.failures > 0 ? <span className="tag err"><Icon name="x" />{s.failures} failed</span> : undefined} />
            )) : (
              <div className="fl-ghost" style={{ ...g.step(0), height: NODE_H }}>Plan steps appear here</div>
            )}

            <Node state={auditState} tall={checks.length > 0} style={g.audit} icon={<Icon name="shield" />} title="Independent audit"
              sub={auditItems.length ? `auditor · ${auditState === "running" ? "running" : fmtSpan(aDur)}` : auditState === "skipped" ? "skipped" : "read-only · pending"}>
              {checks.length > 0 && (
                <ul className="mini">
                  {checks.slice(0, 4).map((c, i) => (
                    <li key={i} className={c.passed ? "" : "no"} title={c.check}>{c.passed ? <Icon name="check" /> : <Icon name="x" />}<span>{c.check}</span></li>
                  ))}
                </ul>
              )}
            </Node>

            <Node state={resultState} style={g.result} icon={<Icon name="flag" />} title="Result"
              sub={f ? SHORT[f.outcome] ?? o!.label : "pending"} subClass={o ? `t-${o.tone}` : undefined} />
          </div>
        </div>
      </div>

      <div className="fl-legend" aria-label="Legend">
        <span className="lg-ok"><Glyph name="ccheck" /><em>Done</em></span>
        <span className="lg-run"><Glyph name="loader" /><em>Running</em></span>
        <span className="lg-err"><Glyph name="cx" /><em>Failed</em></span>
        <span className="lg-pend"><Glyph name="cpend" /><em>Pending</em></span>
      </div>
      {g.mode === "h" && (
        <div className="fl-tools">
          <button aria-label="Zoom in" title="Zoom in" onClick={() => step(0.1)}><Glyph name="plus" /></button>
          <button aria-label="Zoom out" title="Zoom out" onClick={() => step(-0.1)}><Glyph name="minus" /></button>
          <button aria-label="Fit to view" title="Fit" onClick={() => setZoom(null)}><Glyph name="fit" /></button>
        </div>
      )}
    </div>
  );
}
