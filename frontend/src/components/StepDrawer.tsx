import { useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "../lib/api";
import { cx } from "../lib/format";
import type { TimelineItem } from "../lib/runState";
import { Icon } from "./Icon";
import { Glyph, fmtClock, fmtOffset, fmtSpan } from "./inspectKit";
import { useOverlays } from "./Overlays";
import "./inspect.css";

type Tab = "input" | "output" | "screen";

/** JSON with key / string / number colouring, as React nodes (no HTML injection). */
function Json({ value }: { value: unknown }) {
  const text = JSON.stringify(value, null, 2) ?? "null";
  const out: ReactNode[] = [];
  const re = /("(?:\\.|[^"\\])*")(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\btrue\b|\bfalse\b|\bnull\b)|([{}[\],:])/g;
  let last = 0, m: RegExpExecArray | null, k = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] && m[2]) out.push(<span key={k++} className="jk">{m[1]}</span>, <span key={k++} className="jp">{m[2]}</span>);
    else if (m[1]) out.push(<span key={k++} className="js">{m[1]}</span>);
    else if (m[3]) out.push(<span key={k++} className="jn">{m[3]}</span>);
    else out.push(<span key={k++} className="jp">{m[4]}</span>);
    last = re.lastIndex;
  }
  out.push(text.slice(last));
  return <>{out}</>;
}

function CopyBtn({ text }: { text: string }) {
  const { toast } = useOverlays();
  const [done, setDone] = useState(false);
  return (
    <button className="btn ghost sm" onClick={async () => {
      try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1400); }
      catch { toast("Couldn't copy to the clipboard"); }
    }}>{done ? <Icon name="check" /> : <Glyph name="copy" />}{done ? "Copied" : "Copy"}</button>
  );
}

const show = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));

interface Props {
  item: TimelineItem | null;
  runId: string;
  t0: number | null;
  onClose: () => void;
  /** Optional: the run's tool steps and a picker, to enable previous / next step navigation. */
  steps?: TimelineItem[];
  onPick?: (it: TimelineItem) => void;
}

/** Inspect one step: what went in, what came out, and what the page looked like (n8n / LangSmith style). */
export function StepDrawer({ item, runId, t0, onClose, steps, onPick }: Props) {
  const { zoom } = useOverlays();
  const [tab, setTab] = useState<Tab>("input");
  const [view, setView] = useState<"table" | "json">("table");
  const closeBtn = useRef<HTMLButtonElement>(null);
  const open = !!item;
  useEffect(() => { setTab("input"); }, [item?.key]);
  useEffect(() => { if (open) closeBtn.current?.focus({ preventScroll: true }); }, [open]);
  const idx = item && steps ? steps.findIndex((s) => s.key === item.key) : -1;
  const prev = idx > 0 ? steps![idx - 1] : null;
  const next = idx >= 0 && idx < steps!.length - 1 ? steps![idx + 1] : null;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (onPick && !/INPUT|TEXTAREA/.test((e.target as HTMLElement)?.tagName ?? "")) {
        if ((e.key === "ArrowUp" || e.key === "k") && prev) { e.preventDefault(); onPick(prev); }
        if ((e.key === "ArrowDown" || e.key === "j") && next) { e.preventDefault(); onPick(next); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, onPick, prev, next]);
  if (!item) return null;

  const obs = typeof item.obs === "object" ? item.obs : null;
  const running = item.obs === "pending";
  const { intent: _intent, ...args } = (item.args ?? {}) as Record<string, unknown>;
  const raw = { tool: item.title, ...(item.text ? { intent: item.text } : {}), args };
  const tabs: [Tab, string][] = [["input", "Input"], ["output", "Output"], ...(obs?.screenshot ? [["screen", "Screenshot"] as [Tab, string]] : [])];
  const callId = item.key.replace(/^call-/, "");
  const tone = item.failed ? "err" : item.who === "verifier" ? "aud" : running ? "busy" : obs?.ok ? "ok" : "";

  return <>
    <div className="dw-scrim" onClick={onClose} aria-hidden="true" />
    <aside className="drawer dw" role="dialog" aria-modal="true" aria-label="Step details">
      <div className="dw-h">
        <div className="dw-top">
          <span className="stepno">step {idx >= 0 ? String(idx + 1).padStart(2, "0") : ""}{idx >= 0 ? " · " : "· "}{callId}</span>
          <div className="right">
            {onPick && <>
              <button className="icon-btn" aria-label="Previous step" title="Previous step (↑)" disabled={!prev} onClick={() => prev && onPick(prev)}><Glyph name="chev" className="flip" /></button>
              <button className="icon-btn" aria-label="Next step" title="Next step (↓)" disabled={!next} onClick={() => next && onPick(next)}><Glyph name="chev" /></button>
            </>}
            <button className="icon-btn" ref={closeBtn} aria-label="Close" title="Close (Esc)" onClick={onClose}><Icon name="x" /></button>
          </div>
        </div>
        <div className="dw-title">
          <span className={cx("dw-ic", tone)}><Icon name={item.icon} /></span>
          <h2 className="dr-title">{item.title}</h2>
          {item.failed ? <span className="pill err"><Glyph name="cx" />Failed</span>
            : running ? <span className="pill live"><Glyph name="loader" className="rot" />Running</span>
            : item.obs === "stopped" ? <span className="pill"><Glyph name="cskip" />Stopped</span>
            : obs ? <span className="pill ok"><Glyph name="ccheck" />Succeeded</span> : null}
          {item.who === "verifier" && <span className="pill"><Icon name="shield" />Auditor</span>}
        </div>
        {item.text && <p className="dw-intent">{item.text}</p>}
        <div className="dw-meta">
          <div><div className="k">Started</div><div className="v">{fmtClock(item.ts, true)}</div></div>
          <div><div className="k">Offset</div><div className="v">{t0 !== null ? `+${fmtOffset(item.ts - t0)}` : "—"}</div></div>
          <div><div className="k">Duration</div><div className="v">{item.endTs ? fmtSpan(item.endTs - item.ts) : running ? "running" : "—"}</div></div>
          <div><div className="k">Actor</div><div className="v">{item.who === "verifier" ? "auditor" : item.who ?? "worker"}</div></div>
        </div>
        <div className="tabs dw-tabs" role="tablist">
          {tabs.map(([k, l]) => <button key={k} role="tab" aria-selected={k === tab} className={cx(k === tab && "on")} onClick={() => setTab(k)}>{l}</button>)}
        </div>
      </div>

      <div className="dw-b">
        {tab === "input" && <>
          <div>
            <div className="dw-sub"><h4>Arguments</h4>
              {Object.keys(args).length > 0 && (
                <div className="right seg" role="group" aria-label="View">
                  <button className={cx(view === "table" && "on")} aria-pressed={view === "table"} onClick={() => setView("table")}>Table</button>
                  <button className={cx(view === "json" && "on")} aria-pressed={view === "json"} onClick={() => setView("json")}>JSON</button>
                </div>
              )}
            </div>
            {!Object.keys(args).length ? <div className="dw-none">This step took no arguments.</div>
              : view === "table" ? (
                <table className="dw-kv"><tbody>
                  {Object.entries(args).map(([k, v]) => <tr key={k}><td>{k}</td><td>{show(v)}</td></tr>)}
                </tbody></table>
              ) : <pre className="dw-code"><Json value={args} /></pre>}
          </div>
          <div>
            <div className="dw-sub"><h4>Raw call</h4><div className="right"><CopyBtn text={JSON.stringify(raw, null, 2)} /></div></div>
            <pre className="dw-code"><Json value={raw} /></pre>
          </div>
        </>}

        {tab === "output" && (obs ? <>
          {!obs.ok && (
            <div className="dw-err"><Glyph name="cx" /><div><b>{obs.errorKind ? obs.errorKind.replace(/_/g, " ") : "error"}</b><span>The worker saw this failure and decided its next step from it.</span></div></div>
          )}
          <div>
            <div className="dw-sub"><h4>Observation</h4><span className="dw-len">{obs.content.length.toLocaleString()} chars</span><div className="right"><CopyBtn text={obs.content} /></div></div>
            <pre className="dw-code plain">{obs.content || "(empty)"}</pre>
          </div>
        </> : <div className="dw-none">{running ? "Still running…" : item.obs === "stopped" ? "The run ended before this step returned." : "No output was recorded."}</div>)}

        {tab === "screen" && obs?.screenshot && (
          <div>
            <div className="dw-sub"><h4>Page after this step</h4><span className="dw-len">{obs.screenshot}</span></div>
            <button className="dw-shot" onClick={() => zoom(api.shotUrl(runId, obs.screenshot!))} aria-label="Enlarge screenshot">
              <img src={api.shotUrl(runId, obs.screenshot)} alt="Page after this step" />
            </button>
          </div>
        )}
      </div>
    </aside>
  </>;
}
