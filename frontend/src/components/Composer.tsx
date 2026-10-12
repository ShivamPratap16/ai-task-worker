import { useEffect, useRef, useState, type ReactNode } from "react";
import { useStored } from "../hooks/useApp";
import { api } from "../lib/api";
import { exampleIcon, POLICY, type ApprovalMode } from "../lib/constants";
import { cx } from "../lib/format";
import type { Quota, RunSummary } from "../lib/types";
import { Icon } from "./Icon";
import { Hand } from "./Overview";
import { useOverlays } from "./Overlays";
import "./home.css";

function QuotaNote({ quota }: { quota: Quota | null }) {
  if (!quota?.models.length) return null;
  const primary = quota.models[0];
  const active = quota.models.find((m) => m.model === quota.active);
  if (!active) return (
    <div className="quota-note err"><Icon name="alert" /><div>Every configured model has used its free quota for today, so runs will fail.
      {" "}{primary.model} resets in {primary.resets_in ?? "a few hours"}.</div></div>);
  if (primary.exhausted) return (
    <div className="quota-note"><Icon name="alert" /><div>{primary.model} has used its free quota for today (resets in {primary.resets_in}).
      {" "}Runs will use <b>{active.model}</b> instead.</div></div>);
  if (primary.daily_limit && primary.daily_limit - primary.requests < 25) return (
    <div className="quota-note"><Icon name="alert" /><div>Only {primary.daily_limit - primary.requests} requests left today on {primary.model};
      {" "}a task usually needs 10–40. Backup models take over when it runs out.</div></div>);
  return null;
}

/** A short, distinct title for an example task, from what it asks and who it names. */
function exampleTitle(task: string): string {
  const vendor = /(?:from|to|the)\s+([A-Z][\w]*(?:\s+[A-Z][\w]*)?)/.exec(task)?.[1];
  if (/bank/i.test(task)) return "Handle a bank-details change";
  if (/how much|owe|total/i.test(task)) return "Total pending invoices";
  if (/^reply/i.test(task)) return vendor ? `Reply to ${vendor}` : "Reply to a vendor";
  if (/every|all/i.test(task)) return "Log all new invoices";
  if (/latest/i.test(task) && vendor) return `Latest ${vendor} invoice`;
  return vendor ? `Enter the ${vendor} invoice` : "Enter a vendor invoice";
}

const FLOW: [ReactNode, string, string][] = [
  [<Icon key="i" name="list" />, "Plan", "Breaks your request into a short checklist you can review."],
  [<Icon key="i" name="pointer" />, "Act", "Operates a real browser in your sandbox apps, step by step."],
  [<Hand key="i" className="i" />, "Ask", "Stops for approval on risky actions or when unsure."],
  [<Icon key="i" name="shield" />, "Verify", "A separate auditor re-checks the result with evidence."],
];

interface Props {
  examples: string[];
  prefill: string;
  runs: RunSummary[];
  quota: Quota | null;
  onStarted: (runId: string) => void;
}

export function Composer({ examples, prefill, quota, onStarted }: Props) {
  const { toast } = useOverlays();
  const [task, setTask] = useState(prefill);
  const [mode, setMode] = useStored("approval", "risky");
  const [headed, setHeaded] = useStored("headed", "0");
  const [planReview, setPlanReview] = useStored("planReview", "0");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const policy = POLICY[(mode as ApprovalMode) in POLICY ? (mode as ApprovalMode) : "risky"];

  useEffect(() => { setTask(prefill); }, [prefill]);
  useEffect(() => {
    const ta = ref.current;
    if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
  }, []);

  async function start() {
    const text = task.trim();
    if (!text) { ref.current?.focus(); toast("Describe the task first."); return; }
    setBusy(true);
    try {
      const run = await api.start({ task: text, approval_mode: mode, headless: headed !== "1", plan_review: planReview === "1" });
      try { if ("Notification" in window && Notification.permission === "default") void Notification.requestPermission(); } catch { /* unsupported */ }
      onStarted(run.run_id);
    } catch (e) {
      toast((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="home-view">
      <div className="composer">
        <h1>What should the worker do?</h1>
        <p className="lede">Describe the outcome in plain language. The worker plans, operates the browser, asks before risky
          actions, and an independent auditor checks the result.</p>

        <div className="taskbox">
          <label className="sr" htmlFor="task">Task</label>
          <textarea id="task" ref={ref} value={task}
            placeholder="e.g. Find the latest invoice from Globex, extract the amount and due date, and enter it into FinOps"
            onChange={(e) => setTask(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void start(); } }} />
          <div className="taskbar">
            <div className="policy">
              <div className="seg" id="policy" role="group" aria-label="Approval policy">
                {(Object.keys(POLICY) as ApprovalMode[]).map((k) => (
                  <button key={k} data-v={k} className={cx(k === mode && "on")} aria-pressed={k === mode} onClick={() => setMode(k)}>
                    {POLICY[k].label}
                  </button>
                ))}
              </div>
            </div>
            <label className="switch" title="Open a visible browser window on this machine">
              <input type="checkbox" id="headed" checked={headed === "1"} onChange={(e) => setHeaded(e.target.checked ? "1" : "0")} />
              <span className="track" />Show browser
            </label>
            <label className="switch" title="The worker shows you its plan and waits for your OK before acting">
              <input type="checkbox" id="planReview" checked={planReview === "1"} onChange={(e) => setPlanReview(e.target.checked ? "1" : "0")} />
              <span className="track" />Review plan first
            </label>
            <span className="grow" />
            <button className="btn primary run-btn" id="start" disabled={busy} onClick={() => void start()}>
              {busy ? <><span className="spin" />Starting…</> : <>Run task<kbd>Ctrl ↵</kbd></>}
            </button>
          </div>
        </div>
        <p className="hint" id="policyHint"><Icon name="shield" className="i-14" />{policy.hint}</p>
        <QuotaNote quota={quota} />

        {examples.length > 0 && <>
          <div className="section-h"><h2>Start from an example</h2><span className="muted small">Sandbox: Acme Mail, FinOps</span></div>
          <div className="examples">
            {examples.map((t) => {
              const ic = exampleIcon(t);
              return (
                <button key={t} className="example ex" onClick={() => { setTask(t); ref.current?.focus(); }}>
                  <span className="ico"><Icon name={ic} /></span>
                  <b>{exampleTitle(t)}</b>
                  <span>{t}</span>
                </button>
              );
            })}
          </div>
        </>}

        <div className="section-h"><h2>How it works</h2></div>
        <div className="how">
          {FLOW.map(([icon, title, text], i) => (
            <div key={title}><span className="n">{icon}0{i + 1}</span><b>{title}</b><p>{text}</p></div>
          ))}
        </div>
      </div>
    </div>
  );
}
