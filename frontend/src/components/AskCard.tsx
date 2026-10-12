import { useEffect, useRef, useState } from "react";
import { useNow } from "../hooks/useApp";
import { api } from "../lib/api";
import { cx } from "../lib/format";
import type { Pending } from "../lib/runState";
import { Icon } from "./Icon";
import { useOverlays } from "./Overlays";
import { argChips, XIcon, type ExtraIcon } from "./Timeline";

/** [badge icon, badge, title]. The titles are what operators (and tests) look for. */
const HEAD: Record<string, [ExtraIcon | "list", string, string]> = {
  approval: ["hand", "Approval needed", "The worker paused before a high-impact action"],
  plan_review: ["list", "Review plan", "Review the plan before the worker starts"],
};
const QUESTION: [ExtraIcon, string, string] = ["help", "Question", "The worker has a question"];

function waited(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}:${String(s % 60).padStart(2, "0")}`;
}

/** "press 'Submit for payment' on http://…/erp/invoices/2" → sentence case, with the control and the URL as code. */
function richAction(text: string) {
  const t = text.charAt(0).toUpperCase() + text.slice(1);
  return t.split(/('[^']+'|"[^"]+"|“[^”]+”|https?:\/\/\S+)/).map((part, i) => {
    if (i % 2 === 0) return part;
    const quoted = /^['"“]/.test(part);
    return <code key={i}>{quoted ? part.slice(1, -1) : part}</code>;
  });
}

const clockTime = (epoch: number) =>
  new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

interface Props {
  runId: string;
  requestId: string;
  req: Pending;
  autoFocus: boolean;
  /** When the worker asked (epoch seconds), for the "waiting 0:47" counter. */
  since?: number;
  /** Arguments of the tool call awaiting approval, shown as chips. */
  args?: Record<string, unknown>;
}

/** What the worker is waiting on: an approval, a plan review, or a question. Pinned above the timeline. */
export function AskCard({ runId, requestId, req, autoFocus, since, args }: Props) {
  const { toast, zoom } = useOverlays();
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const input = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  const now = useNow(since !== undefined);
  const [icon, badge, title] = HEAD[req.kind] ?? QUESTION;
  const isApproval = req.kind === "approval", isPlan = req.kind === "plan_review";
  const chips = argChips(args, 60);

  useEffect(() => { if (autoFocus) input.current?.focus({ preventScroll: true }); }, [autoFocus]);

  async function reply(approved: boolean | null, preset?: string, needsNote = false) {
    const answer = (preset ?? note).trim();
    if (needsNote && !answer) { toast("Describe the changes you want first."); input.current?.focus(); return; }
    setSending(true);
    try { await api.respond(runId, { request_id: requestId, approved, answer }); }
    catch (e) { toast((e as Error).message); setSending(false); }
  }

  const onEnter = (e: React.KeyboardEvent) => {
    // Enter sends free-text answers only; approvals always need an explicit click.
    if (e.key === "Enter" && !isApproval && !isPlan) { e.preventDefault(); void reply(null); }
  };

  const steps = req.steps ?? [];
  const meta = isPlan ? `${steps.length} step${steps.length === 1 ? "" : "s"}` : since !== undefined ? `waiting ${waited(now - since)}` : "";

  return (
    <div className={cx("card ask need", `need-${isApproval ? "approval" : isPlan ? "plan" : "question"}`)} role="region" aria-label={title}>
      <div className="need-h">
        <span className="badge">{icon === "list" ? <Icon name="list" /> : <XIcon name={icon} />}{badge}</span>
        <span className="ttl">{title}</span>
        {meta && <span className="wait mono">{meta}</span>}
      </div>
      <div className={cx("need-b", !req.screenshot && "one")}>
        <div className="need-main">
          {!isPlan && <p className="need-q">{isApproval ? richAction(req.question) : req.question}</p>}
          {req.reason && <p className="why"><Icon name="shield" /><span>{req.reason}</span></p>}
          {isApproval && !!chips.length && <div className="kv-line">{chips}</div>}

          {isApproval && <>
            <p className="why conseq"><Icon name="alert" /><span>If you deny, the worker will not do this and will say so in its report.</span></p>
            <div className="note">
              <input ref={input} className="input" type="text" aria-label="Note to the worker" placeholder="Optional note to the worker…"
                value={note} onChange={(e) => setNote(e.target.value)} />
              <button className="btn" disabled={sending} onClick={() => void reply(false)}>Deny</button>
              <button className="btn primary" disabled={sending} onClick={() => void reply(true)}><Icon name="check" />Approve</button>
            </div>
          </>}

          {isPlan && <>
            <ol className="plan-ol">
              {steps.map((s, i) => <li key={i}><span className="n">{String(i + 1).padStart(2, "0")}</span><div>{s.step}</div></li>)}
            </ol>
            <textarea ref={input} className="input" aria-label="Requested changes" value={note} onChange={(e) => setNote(e.target.value)}
              placeholder="Request changes, e.g. “Use the AP queue, not the general ledger”" />
            <div className="note end">
              <button className="btn" disabled={sending} onClick={() => void reply(false, undefined, true)}>Request changes</button>
              <button className="btn primary" disabled={sending} onClick={() => void reply(true)}><Icon name="check" />Approve plan</button>
            </div>
          </>}

          {!isApproval && !isPlan && <>
            {!!req.options?.length && (
              <div className="opts">
                {req.options.map((o, i) => (
                  <button key={o} className="opt" disabled={sending} onClick={() => void reply(null, o)}>
                    <span className="k" aria-hidden="true">{i + 1}</span><b>{o}</b>
                  </button>
                ))}
              </div>
            )}
            <div className="note">
              <input ref={input} className="input" type="text" aria-label="Your answer"
                placeholder={req.options?.length ? "Or type your own answer…" : "Type an answer…"}
                value={note} onChange={(e) => setNote(e.target.value)} onKeyDown={onEnter} />
              <button className="btn primary" disabled={sending} onClick={() => void reply(null)}><XIcon name="up" />Send</button>
            </div>
          </>}
        </div>
        {req.screenshot && (
          <div className="shot-col">
            <button className="shot-frame" onClick={() => zoom(api.shotUrl(runId, req.screenshot!))} aria-label="Open screenshot">
              <img className="shot" src={api.shotUrl(runId, req.screenshot)} alt="The page when the worker asked" title="The page when the worker asked" />
            </button>
            <div className="shot-cap"><span>The page when it asked</span>{since !== undefined && <span className="mono">{clockTime(since)}</span>}</div>
          </div>
        )}
      </div>
    </div>
  );
}
