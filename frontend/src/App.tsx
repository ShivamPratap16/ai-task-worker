import { useEffect, useRef, useState } from "react";
import { Composer } from "./components/Composer";
import { Icon } from "./components/Icon";
import { Inspector } from "./components/Inspector";
import { useOverlays } from "./components/Overlays";
import { Overview } from "./components/Overview";
import { RunView } from "./components/RunView";
import { Logo, Sidebar } from "./components/Sidebar";
import { useHashRoute, usePolling, useTheme } from "./hooks/useApp";
import { useRunStream } from "./hooks/useRunStream";
import { api } from "./lib/api";
import { outcomeOf } from "./lib/constants";
import { cx } from "./lib/format";
import type { RunSummary } from "./lib/types";

const OVERVIEW = "overview";  // a hash route; run ids are timestamps, so they never collide
const ASK_TITLE: Record<string, string> = { approval: "Approval needed", plan_review: "Plan ready for your review" };

/** Tab title badge plus desktop notifications when the tab is in the background. */
function useAttention(runs: RunSummary[], current: string | null, livePending: string[]) {
  const seen = useRef<{ pending: Set<string>; done: Set<string> } | null>(null);
  useEffect(() => {
    const notify = (title: string, body: string, runId: string) => {
      if (!document.hidden) return;
      try {
        if (!("Notification" in window) || Notification.permission !== "granted") return;
        const n = new Notification(title, { body, tag: `${runId}-${title}` });
        n.onclick = () => { window.focus(); location.hash = runId; n.close(); };
      } catch { /* notifications unsupported */ }
    };
    const pend = runs.flatMap((r) => r.pending.map((p) => ({ run: r, p })));
    const done = new Set(runs.filter((r) => r.outcome).map((r) => r.run_id));
    if (seen.current) {
      for (const { run, p } of pend) if (!seen.current.pending.has(p.id)) notify(ASK_TITLE[p.kind] ?? "The worker has a question", p.question, run.run_id);
      for (const r of runs) if (r.outcome && !seen.current.done.has(r.run_id)) notify(outcomeOf(r.outcome).label, r.task, r.run_id);
    }
    seen.current = { pending: new Set(pend.map((x) => x.p.id)), done };
  }, [runs]);

  useEffect(() => {
    // Live stream for the open run (instant) plus the poll for every other run.
    const ids = new Set(runs.filter((r) => r.run_id !== current).flatMap((r) => r.pending.map((p) => p.id)));
    livePending.forEach((id) => ids.add(id));
    document.title = (ids.size ? `(${ids.size}) Needs you · ` : "") + "AI Task Worker";
  }, [runs, current, livePending]);
}

export default function App() {
  const [route, go] = useHashRoute();
  const { runs, quota, refresh } = usePolling();
  const [dark, toggleTheme] = useTheme();
  const { toast } = useOverlays();
  const [examples, setExamples] = useState<string[]>([]);
  const [provider, setProvider] = useState("");
  const [prefill, setPrefill] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const scroller = useRef<HTMLElement>(null);
  const page = route === OVERVIEW ? "overview" : route ? "run" : "new";
  const runId = page === "run" ? route : null;
  const run = useRunStream(runId, runs.find((r) => r.run_id === runId)?.task ?? "");
  const livePending = Object.keys(run.pending);

  useAttention(runs, runId, run.finished ? [] : livePending);

  useEffect(() => {
    api.examples().then((ex) => { setExamples(ex.tasks); setProvider(ex.provider); }).catch(() => setProvider("server unreachable"));
  }, []);
  useEffect(() => { if (run.finished) void refresh(); }, [run.finished, refresh]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = /INPUT|TEXTAREA/.test((document.activeElement as HTMLElement | null)?.tagName ?? "");
      if (e.key === "Escape") setMenuOpen(false);
      if (e.key === "n" && !e.ctrlKey && !e.metaKey && !typing) { e.preventDefault(); newTask(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const open = (id: string) => { go(id); setMenuOpen(false); };
  const newTask = (text = "") => { setPrefill(text); go(null); setMenuOpen(false); };
  const running = !!runId && !run.finished && !run.streamEnded;
  const stop = async () => {
    if (!runId) return;
    try { await api.cancel(runId); toast("Stopping the run…", "ok"); } catch (e) { toast((e as Error).message); }
  };
  const copyId = async () => {
    if (!runId) return;
    try { await navigator.clipboard.writeText(runId); toast("Run ID copied", "ok"); } catch { toast("Could not copy the run ID"); }
  };

  return (
    <div className="app" id="app">
      <Sidebar runs={runs} current={runId} provider={provider} quota={quota} open={menuOpen} dark={dark} page={page}
        onOpen={open} onNew={() => newTask()} onOverview={() => open(OVERVIEW)} onToggleTheme={toggleTheme} />
      <div className={cx("side-scrim", menuOpen && "on")} aria-hidden="true" onClick={() => setMenuOpen(false)} />
      <div className="shell">
        <div className="mobile-top">
          <Logo />
          <div className="ws-txt"><b>Task Worker</b><span>Acme Corp sandbox</span></div>
          <div className="right">
            <button className="btn primary sm icon" aria-label="New task" onClick={() => newTask()}><Icon name="plus" /></button>
            <button className="btn sm icon" id="menuBtn" aria-label="Menu" aria-expanded={menuOpen} onClick={() => setMenuOpen((o) => !o)}><Icon name="menu" /></button>
          </div>
        </div>
        <header className={cx("topbar", `page-${page}`)}>
          {page === "run" && runId ? <>
            <nav className="crumbs" aria-label="Breadcrumb">
              <a href="#overview" onClick={(e) => { e.preventDefault(); open(OVERVIEW); }}>Runs</a>
              <span className="sep">/</span>
              <span className="cur mono" title={runId}>{runId}</span>
              <button className="icon-btn hide-sm" aria-label="Copy run ID" title="Copy run ID" onClick={copyId}><Icon name="copy" /></button>
            </nav>
            <div className="right">
              {running && <button className="btn sm" onClick={stop} title="Stop this run"><Icon name="stop" /><span className="lbl">Stop</span></button>}
              {!running && run.task && <button className="btn sm" onClick={() => newTask(run.task)} title="Start a new run with this task"><Icon name="rerun" /><span className="lbl">Run again</span></button>}
            </div>
          </> : <>
            <nav className="crumbs" aria-label="Breadcrumb"><span className="cur">{page === "overview" ? "Overview" : "New task"}</span></nav>
            <div className="right">
              {page === "new" && <span className="muted small hide-sm">Policy applies to this run only</span>}
              {page === "overview" && <button className="btn sm" onClick={() => newTask()}><Icon name="plus" /><span className="lbl">New task</span></button>}
            </div>
          </>}
        </header>
        <div className={cx("workspace", page === "run" && "with-inspector")}>
          <main className="main" id="mainScroll" ref={scroller}>
            {page === "overview" ? <Overview runs={runs} quota={quota} onOpen={open} onNew={() => newTask()} />
              : runId ? <RunView key={runId} runId={runId} run={run} scroller={scroller} onRerun={newTask} />
              : <Composer examples={examples} prefill={prefill} runs={runs} quota={quota} onStarted={(id) => { void refresh(); open(id); }} />}
          </main>
          <aside className="inspector" id="inspector" aria-label="Run inspector">{runId && <Inspector run={run} runId={runId} />}</aside>
        </div>
      </div>
    </div>
  );
}
