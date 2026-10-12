import { useState } from "react";
import { api } from "../lib/api";
import { outcomeOf } from "../lib/constants";
import { cx, fmtDur } from "../lib/format";
import type { Shot } from "../lib/runState";
import type { Finished } from "../lib/types";
import { Icon } from "./Icon";
import { useOverlays } from "./Overlays";
import { XIcon } from "./Timeline";

/** "Invoice number: GX-2291" → key/value row; anything else stays a plain line. */
function splitClaim(c: string): [string | null, string] {
  const m = /^([^:]{2,32}):\s+(.+)$/.exec(c);
  return m ? [m[1], m[2]] : [null, c];
}

const isLink = (s: string) => /^(\/|https?:\/\/)\S+$/.test(s);

interface Props { runId: string; f: Finished; shots?: Shot[] }

export function ResultCard({ runId, f, shots = [] }: Props) {
  const { zoom } = useOverlays();
  const [open, setOpen] = useState(true);
  const o = outcomeOf(f.outcome);
  const v = f.verification;
  const passed = v?.checks.filter((c) => c.passed).length ?? 0;
  const total = v?.checks.length ?? 0;
  const pillTone = o.tone === "none" ? "" : o.tone;
  const pill = o.tone === "ok" && total ? `Verified ${passed}/${total}` : o.label;
  const evidenceShots = shots.slice(-2);
  const claims = f.claims ?? [];
  const evidence = f.evidence ?? [];
  const changes = f.changes;
  const hasLeft = claims.length > 0 || evidence.length > 0 || evidenceShots.length > 0 || changes !== undefined;

  return (
    <div className={cx("card result", o.tone, !open && "collapsed")}>
      <div className="result-h">
        <span className={cx("pill", pillTone)}><Icon name={o.icon} />{pill}</span>
        <span className="summary">{f.summary || o.label}</span>
        <div className="right">
          <a className="btn sm" href={api.reportUrl(runId)} download><Icon name="down" />Download report</a>
          <button className="icon-btn sm" aria-label={open ? "Collapse result" : "Expand result"} aria-expanded={open} onClick={() => setOpen(!open)}>
            <XIcon name="chevd" />
          </button>
        </div>
      </div>
      {open && (hasLeft || v) && (
        <div className={cx("result-b", !(hasLeft && v) && "one")}>
          {hasLeft && (
            <div>
              {!!claims.length && <>
                <div className="label"><span>Claims</span></div>
                <ul className="claims">
                  {claims.map((c, i) => {
                    const [k, val] = splitClaim(c);
                    return <li key={i} className={k ? "" : "plain"}>{k && <span className="k">{k}</span>}<span className="v">{val}</span></li>;
                  })}
                </ul>
              </>}
              {changes !== undefined && <>
                <div className="label"><span>Changes made</span><span className="mono lc">from the execution log</span></div>
                <ul className="claims">
                  {changes.length
                    ? changes.map((c, i) => <li key={i}><span className="k">{c.action}</span><span className="v">{c.page.replace(/^https?:\/\/[^/]+/, "")}</span></li>)
                    : <li className="plain"><span className="v">None: nothing in company systems was changed.</span></li>}
                </ul>
              </>}
              {(!!evidence.length || !!evidenceShots.length) && (
                <div className="evidence">
                  {evidenceShots.map((s) => (
                    <button key={s.name} className="shot-frame" onClick={() => zoom(api.shotUrl(runId, s.name))} aria-label="Open evidence screenshot" title={s.url ?? s.name}>
                      <img src={api.shotUrl(runId, s.name)} alt="" loading="lazy" />
                    </button>
                  ))}
                  {!!evidence.length && (
                    <div className="ev-links">
                      {evidence.map((e, i) => isLink(e)
                        ? <span key={i} className="ev-link"><XIcon name="ext" className="i-12" />{e}</span>
                        : <span key={i} className="ev-line">{e}</span>)}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
          {v && (
            <div>
              <div className="label"><span>Independent verification</span><span className="mono lc">auditor · {v.confidence} confidence</span></div>
              <ul className="checks">
                {v.checks.map((c, i) => (
                  <li key={i} className={c.passed ? "pass" : "fail"}>
                    <XIcon name={c.passed ? "ccheck" : "cx"} />
                    <div><b>{c.check}</b>{c.evidence && <span>{c.evidence}</span>}</div>
                  </li>
                ))}
                {v.issues.map((t, i) => (
                  <li key={`i${i}`} className="issue"><Icon name="alert" /><div><b>{t}</b></div></li>
                ))}
              </ul>
              <div className="verdict">
                <Icon name="shield" className={v.achieved ? "ok" : "err"} />
                Auditor verdict: <b>{v.achieved ? "pass" : "fail"}</b>
                {f.steps !== undefined && <span className="faint">· {f.steps} steps · {fmtDur(f.duration_s ?? 0)}</span>}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
