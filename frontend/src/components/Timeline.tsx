import { memo } from "react";
import { api } from "../lib/api";
import { cx } from "../lib/format";
import type { Category, TimelineItem } from "../lib/runState";
import type { Check } from "../lib/types";
import { Icon } from "./Icon";
import { useOverlays } from "./Overlays";
import "./run.css";

export type Filter = "all" | Category;
export const FILTERS: [Filter, string][] = [["all", "All"], ["steps", "Actions"], ["reasoning", "Reasoning"], ["issues", "Issues"], ["verifier", "Verifier"]];

/* A few line icons the shared set doesn't have (same 24x24 stroked style as <Icon/>). */
const EXTRA = {
  hand: '<path d="M18 11V6a2 2 0 0 0-4 0M14 10V4a2 2 0 0 0-4 0v2M10 10.5V6a2 2 0 0 0-4 0v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-6-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15"/>',
  help: '<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/>',
  user: '<circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/>',
  loader: '<path d="M21 12a9 9 0 1 1-6.2-8.56"/>',
  chevd: '<path d="m6 9 6 6 6-6"/>',
  up: '<path d="m5 12 7-7 7 7M12 19V5"/>',
  ccheck: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
  cx: '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/>',
  ext: '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
} as const;
export type ExtraIcon = keyof typeof EXTRA;

export function XIcon({ name, className }: { name: ExtraIcon; className?: string }) {
  return <svg className={className ? `i ${className}` : "i"} viewBox="0 0 24 24" aria-hidden="true" dangerouslySetInnerHTML={{ __html: EXTRA[name] }} />;
}

/** mm:ss since the run started (the timeline's left column). */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  const mmss = `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  return h ? `${h}:${mmss}` : mmss;
}

/** Step duration: "2.8s" under a minute, "1m 02s" above. */
export function secs(seconds: number): string {
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  return `${m}m ${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
}

export function argChips(args?: Record<string, unknown>, max = 80) {
  return Object.entries(args ?? {}).filter(([k]) => k !== "intent").map(([k, v]) => {
    const s = typeof v === "string" ? v : JSON.stringify(v);
    return <span key={k} className="chip arg" title={`${k}: ${s}`}><b>{k}</b>{s.length > max ? `${s.slice(0, max)}…` : s}</span>;
  });
}

export function CheckList({ checks = [], issues = [] }: { checks?: Check[]; issues?: string[] }) {
  return (
    <ul className="ck">
      {checks.map((c, i) => (
        <li key={`c${i}`} className={c.passed ? "pass" : "fail"}>
          <Icon name={c.passed ? "check" : "x"} /><span className="t">{c.check}</span>{c.evidence && <span className="e">{c.evidence}</span>}
        </li>
      ))}
      {issues.map((t, i) => <li key={`i${i}`} className="issue"><Icon name="alert" /><span className="t">{t}</span></li>)}
    </ul>
  );
}

const NODE_TONE: Record<string, string> = { error: "err", warn: "warn", good: "ok", ver: "acc" };

interface ItemProps { it: TimelineItem; runId: string; t0: number | null; last: boolean; onSelect: (it: TimelineItem) => void }

const Item = memo(function Item({ it, runId, t0, last, onSelect }: ItemProps) {
  const { zoom } = useOverlays();
  const obs = it.obs;
  const isCall = it.key.startsWith("call-");
  const isAction = isCall;
  const running = obs === "pending";
  const done = typeof obs === "object" ? obs : null;
  const dur = it.endTs !== undefined ? it.endTs - it.ts : undefined;
  const node = running ? "acc" : NODE_TONE[it.tone] ?? "";
  const boxTone = it.tone === "error" ? "err" : it.tone === "warn" ? "warn" : "";
  const chips = argChips(it.args);
  const firstLine = done?.content ? done.content.split("\n")[0].slice(0, 200) : "";
  const stop = (e: React.SyntheticEvent) => e.stopPropagation();

  return (
    <div className={cx("ev tl-item", it.tone && `k-${it.tone}`, it.failed && "failed", isCall && "clickable", last && "last", ...it.cats.map((c) => `c-${c}`))}
      id={isCall ? it.key : undefined}>
      <span className="tl-time">{t0 ? clock(it.ts - t0) : ""}</span>
      <span className={cx("tl-node", node)}>{it.tone === "you" ? <XIcon name="user" /> : <Icon name={it.icon} />}</span>
      <div className="box tl-body" onClick={isCall ? () => onSelect(it) : undefined}>
        <div className="tl-row">
          <div className="tl-main">
            <div className="tl-head">
              <b>{it.title}</b>
              {it.who === "verifier" && <span className="tl-who">auditor</span>}
              {isAction && it.text && <span className="intent">{it.text}</span>}
              {it.tone === "you" && <span className="intent faint">message to the worker</span>}
              {!isAction && it.note && it.tone !== "you" && <span className="intent">{it.note}</span>}
            </div>
            {!!chips.length && <div className="tl-chips">{chips}</div>}
          </div>
          {(obs || isCall) && (
            <div className="tl-meta">
              {isCall && (
                <button className="inspect" onClick={(e) => { stop(e); onSelect(it); }} title="Inspect input and output" aria-label={`Inspect ${it.title}`}>
                  <Icon name="eye" /><span>Inspect</span>
                </button>
              )}
              {running ? <span className="st live"><XIcon name="loader" className="rot" />Running</span>
                : obs === "stopped" ? <span className="st"><Icon name="stop" />Stopped</span>
                : done ? <span className={cx("st", done.ok ? "ok" : "err")}><Icon name={done.ok ? "check" : "x"} />{done.ok ? "Done" : "Failed"}</span>
                : null}
              {dur !== undefined && <span className="dur">{secs(dur)}</span>}
              {done?.screenshot && (
                <button className="tl-thumb" onClick={(e) => { stop(e); zoom(api.shotUrl(runId, done.screenshot!)); }} aria-label="Open screenshot">
                  <img src={api.shotUrl(runId, done.screenshot)} alt="" loading="lazy" />
                </button>
              )}
            </div>
          )}
        </div>

        {it.reasoning && (it.reasoning.length <= 220 && !it.reasoning.includes("\n")
          ? <p className="tl-text">{it.reasoning}</p>
          : <details className="tl-reason" onClick={stop}>
            <summary>{it.reasoning.slice(0, 220)}{it.reasoning.length > 220 ? "…" : ""}</summary>
            <pre>{it.reasoning}</pre>
          </details>
        )}
        {!isAction && it.text && (
          it.tone === "you" ? <div className="bubble">{it.text}</div>
            : boxTone ? <div className={`tl-box ${boxTone}`}>{it.text}</div>
            : <p className="tl-text">{it.text}</p>
        )}
        {it.tone === "you" && it.note && <div className="tl-note">{it.note}</div>}
        {isAction && it.note && <div className="tl-note">{it.note}</div>}
        {done && !done.ok && (
          <div className="tl-box err">
            {done.errorKind && <code>{done.errorKind}</code>} {firstLine}
            {done.content.includes("\n") && <details onClick={stop}><summary>Full output</summary><pre>{done.content}</pre></details>}
          </div>
        )}
        {done?.ok && done.content && isCall && (
          <details className="tl-out" onClick={stop}><summary>{firstLine}</summary><pre>{done.content}</pre></details>
        )}
        {(it.checks || it.issues) && <CheckList checks={it.checks} issues={it.issues} />}
      </div>
    </div>
  );
});

interface Props { items: TimelineItem[]; runId: string; t0: number | null; filter: Filter; onSelect: (it: TimelineItem) => void; live?: boolean }

export function Timeline({ items, runId, t0, filter, onSelect, live = false }: Props) {
  const shown = filter === "all" ? items : items.filter((it) => it.cats.includes(filter));
  return (
    <div className="tl" id="timeline" data-filter={filter}>
      {shown.map((it, i) => <Item key={it.key} it={it} runId={runId} t0={t0} last={i === shown.length - 1} onSelect={onSelect} />)}
      {!shown.length && (items.length || !live) && <div className="empty-s tl-empty">{items.length ? "Nothing in this filter yet." : "No activity yet."}</div>}
    </div>
  );
}
