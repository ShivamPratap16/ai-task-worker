import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { api } from "../lib/api";
import { cx } from "../lib/format";
import type { RunState, TimelineItem } from "../lib/runState";
import { AskCard } from "./AskCard";
import { Icon } from "./Icon";
import { Inspector } from "./Inspector";
import { useOverlays } from "./Overlays";
import { ResultCard } from "./ResultCard";
import { FlowView } from "./FlowView";
import { RunHeader } from "./RunHeader";
import { StepDrawer } from "./StepDrawer";
import { TraceView } from "./TraceView";
import { FILTERS, Timeline, XIcon, type Filter } from "./Timeline";

function SteerBar({ runId }: { runId: string }) {
  const { toast } = useOverlays();
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  return (
    <form className="steer" id="steer" onSubmit={async (e) => {
      e.preventDefault();
      const t = text.trim(); if (!t) return;
      setSending(true);
      try { await api.message(runId, t); setText(""); } catch (err) { toast((err as Error).message); }
      finally { setSending(false); input.current?.focus(); }
    }}>
      <div className="steer-box">
        <Icon name="msg" />
        <input id="steerInput" ref={input} value={text} disabled={sending} autoComplete="off" aria-label="Message the worker"
          placeholder="Message the worker, e.g. “use the Globex Corp vendor record”" onChange={(e) => setText(e.target.value)} />
        <button className="btn primary sm send" type="submit" aria-label="Send message" disabled={sending}><XIcon name="up" /></button>
      </div>
      <div className="hint">The worker reads your message before its next step. <kbd>Enter</kbd> to send</div>
    </form>
  );
}

/** Timeline entries that announce each kind of request, newest last (to date the "waiting" counters). */
const ASK_TITLES: Record<string, string> = { approval: "Approval requested", plan_review: "Plan sent for your review" };

interface Props {
  runId: string;
  run: RunState;
  scroller: RefObject<HTMLElement>;
  /** Kept for the shell's call site; Run again lives in the top bar now. */
  onRerun?: (task: string) => void;
}

export function RunView({ runId, run, scroller }: Props) {
  const [filter, setFilter] = useState<Filter>("all");
  const [view, setView] = useState<"activity" | "trace" | "flow">("activity");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const select = useCallback((it: TimelineItem) => setSelectedKey(it.key), []);
  const selected = selectedKey ? run.items.find((it) => it.key === selectedKey) ?? null : null;
  const [follow, setFollow] = useState(true);
  const pendingIds = Object.keys(run.pending);
  const pendingKey = pendingIds.join();
  const running = !run.finished && !run.streamEnded;

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: run.items.length };
    for (const it of run.items) for (const cat of it.cats) c[cat] = (c[cat] ?? 0) + 1;
    return c;
  }, [run.items]);

  // When each pending request was raised, and (for approvals) the arguments of the call waiting on it.
  const askMeta = useMemo(() => {
    const meta: Record<string, { since?: number; args?: Record<string, unknown> }> = {};
    const items = run.items;
    for (const id of Object.keys(run.pending)) {
      const req = run.pending[id];
      const title = ASK_TITLES[req.kind] ?? "Worker asks";
      const rev = [...items].reverse();
      const ask = rev.find((it) => it.title === title && (title !== "Worker asks" || it.text === req.question)) ?? rev.find((it) => it.title === title);
      const call = req.kind === "approval" ? [...items].reverse().find((it) => it.obs === "pending" && it.args) : undefined;
      meta[id] = { since: ask?.ts, args: call?.args };
    }
    return meta;
  }, [run.pending, run.items]);

  useEffect(() => { setFollow(true); setFilter("all"); setView("activity"); setSelectedKey(null); }, [runId]);

  // Follow new activity unless the operator scrolled up to read.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (follow && !pendingKey && el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [run.items.length, follow, pendingKey, scroller, view]);

  useEffect(() => {
    const el = scroller.current; if (!el) return;
    const onScroll = () => setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 160);
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [scroller]);

  // A new request for the operator: bring the (pinned) card into view.
  useEffect(() => {
    if (pendingKey) scroller.current?.scrollTo({ top: 0, behavior: "smooth" });
  }, [pendingKey, scroller]);

  return (
    <div className="run">
      <RunHeader run={run} />
      <div className="run-body">
        {!!pendingIds.length && (
          <div id="pendingBox" className="pending">
            {pendingIds.map((id, i) => (
              <AskCard key={id} runId={runId} requestId={id} req={run.pending[id]} autoFocus={i === 0} since={askMeta[id]?.since} args={askMeta[id]?.args} />
            ))}
          </div>
        )}
        {run.finished && <ResultCard runId={runId} f={run.finished} shots={run.shots} />}
        <div className="inline-inspector"><Inspector run={run} runId={runId} prefix="m-" /></div>

        <section className="run-tabs">
          <div className="tabs-bar">
            <div className="tabs" role="tablist">
              <button role="tab" aria-selected={view === "activity"} className={cx(view === "activity" && "on")} onClick={() => setView("activity")}>
                <Icon name="steps" />Activity<span className="count" id="tlCount">{run.items.length}</span>
              </button>
              <button role="tab" aria-selected={view === "trace"} className={cx(view === "trace" && "on")} onClick={() => setView("trace")}><Icon name="clock" />Trace</button>
              <button role="tab" aria-selected={view === "flow"} className={cx(view === "flow" && "on")} onClick={() => setView("flow")}><Icon name="zap" />Flow</button>
            </div>
            {view === "activity" && running && (
              <div className="right">
                <button className={cx("btn ghost sm follow", follow && "on")} aria-pressed={follow} onClick={() => setFollow(true)}
                  title={follow ? "Following new activity" : "Jump to the latest activity"}>
                  <Icon name="down" />Follow live
                </button>
              </div>
            )}
          </div>

          {view === "activity" && <>
            <div className="filters" id="filter" role="group" aria-label="Filter activity">
              {FILTERS.map(([f, label]) => (
                <button key={f} data-f={f} className={cx("fbtn", f === filter && "on")} aria-pressed={f === filter} onClick={() => setFilter(f)}>
                  {label}<span className="c">{counts[f] ?? 0}</span>
                </button>
              ))}
            </div>
            <Timeline items={run.items} runId={runId} t0={run.t0} filter={filter} onSelect={select} live={running} />
          </>}
          {view === "trace" && <TraceView run={run} onSelect={select} />}
          {view === "flow" && <FlowView run={run} />}
        </section>

        {running && (
          <div className="thinking">
            <span className="dots"><i /><i /><i /></span>
            {pendingIds.length ? "Waiting for your input above…" : run.items.length ? "Working…" : "Starting the worker…"}
          </div>
        )}
      </div>
      <StepDrawer item={selected} runId={runId} t0={run.t0} onClose={() => setSelectedKey(null)}
        steps={run.items.filter((it) => it.key.startsWith("call-"))} onPick={select} />
      {running && run.t0 !== null && <SteerBar runId={runId} />}
    </div>
  );
}
