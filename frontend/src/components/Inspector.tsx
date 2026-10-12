import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { cx } from "../lib/format";
import type { RunState } from "../lib/runState";
import { Icon } from "./Icon";
import { Glyph, StateGlyph, fmtClock, fmtOffset, fmtSpan, planTiming } from "./inspectKit";
import { useOverlays } from "./Overlays";
import "./inspect.css";

/** Live browser view, plan and working memory. Rendered in the right column, or inline on narrower screens
 *  (`prefix` keeps element ids unique when both copies are in the page). */
export function Inspector({ run, runId, prefix = "" }: { run: RunState; runId: string; prefix?: string }) {
  const { zoom } = useOverlays();
  const [picked, setPicked] = useState<number | null>(null); // null = follow the latest screenshot
  const film = useRef<HTMLDivElement>(null);
  const idx = picked ?? run.shots.length - 1;
  const shot = run.shots[idx];
  const running = !run.finished && !run.streamEnded;
  const following = idx === run.shots.length - 1;

  useEffect(() => { setPicked(null); }, [runId]);
  useEffect(() => {
    const el = film.current?.querySelector<HTMLElement>("[aria-current='true']");
    if (el && film.current) film.current.scrollLeft = el.offsetLeft - film.current.clientWidth / 2 + el.clientWidth / 2;
  }, [idx, run.shots.length]);

  // When each screenshot was taken: the observation that carried it.
  const shotTs = useMemo(() => {
    const m = new Map<string, number>();
    for (const it of run.items) {
      const o = typeof it.obs === "object" ? it.obs : null;
      if (o?.screenshot && !m.has(o.screenshot)) m.set(o.screenshot, it.endTs ?? it.ts);
    }
    return m;
  }, [run.items]);
  const off = (name?: string) => {
    const ts = name ? shotTs.get(name) : undefined;
    return ts !== undefined && run.t0 !== null ? fmtOffset(ts - run.t0) : "";
  };

  const done = run.plan.filter((s) => s.status === "done").length;
  const times = planTiming(run);
  const facts = Object.entries(run.facts);
  const endedAt = run.finished && run.t0 !== null ? run.t0 + (run.finished.duration_s ?? 0) : null;

  return <>
    <section className="ins-sec ins-browser">
      <div className="ins-h">
        <h3>Browser</h3>
        <div className="right">
          {running && following && shot && <span className="ins-live"><span className="ins-pulse" />Live</span>}
          {running && !following && <button className="ins-link" onClick={() => setPicked(null)}>Back to live</button>}
          {!running && endedAt !== null && <span className="mono ins-ended">ended {fmtClock(endedAt)}</span>}
          {shot && (
            <button className="icon-btn ins-icon" aria-label="Expand screenshot" title="Expand"
              onClick={() => zoom(api.shotUrl(runId, shot.name))}><Glyph name="max" /></button>
          )}
        </div>
      </div>
      <div className="ins-frame">
        <div className="ins-bar">
          <span className="ins-dots" aria-hidden="true"><i /><i /><i /></span>
          <span className="ins-url"><Icon name="lock" /><span id={`${prefix}url`}>{shot ? shot.url || "—" : "about:blank"}</span></span>
        </div>
        <div className="ins-screen" id={`${prefix}screen`}>
          {shot
            ? <img key={shot.name} src={api.shotUrl(runId, shot.name)} alt="Browser screenshot" onClick={(e) => zoom(e.currentTarget.src)} />
            : <div className="ins-ph"><Icon name="image" /><span>The live browser view appears<br />as soon as the worker acts.</span></div>}
        </div>
      </div>
      {run.shots.length > 1 && <>
        <div className="ins-film" id={`${prefix}film`} ref={film} role="group" aria-label="Previous screenshots">
          {run.shots.map((s, i) => (
            <button key={`${s.name}-${i}`} aria-current={i === idx ? "true" : undefined} title={[off(s.name), s.url].filter(Boolean).join(" · ")}
              aria-label={`Screenshot ${i + 1}`} onClick={() => setPicked(i === run.shots.length - 1 ? null : i)}>
              <img src={api.shotUrl(runId, s.name)} alt="" loading="lazy" />
            </button>
          ))}
        </div>
        <div className="ins-cap">
          <span>{off(run.shots[0]?.name)}</span>
          <span>{off(shot?.name)}{running && following ? " · now" : ""}{!following ? ` · ${idx + 1} of ${run.shots.length}` : ""}</span>
        </div>
      </>}
    </section>

    <section className="ins-sec">
      <div className="ins-h">
        <h3>Plan</h3>
        <div className="right"><span className="num" id={`${prefix}planCount`}>{run.plan.length ? `${done} of ${run.plan.length}` : ""}</span></div>
      </div>
      <div id={`${prefix}plan`}>
        {run.plan.length ? <>
          <ul className="ins-plan">
            {run.plan.map((s, i) => {
              const live = s.status === "in_progress" && running;
              const state = s.status === "in_progress" && !running ? "stopped" : s.status;
              const t = times[i];
              return (
                <li key={i} className={cx(`st-${s.status}`, live && "now")}>
                  <StateGlyph state={state} />
                  <span className="t">{s.step}</span>
                  <span className="m">{live ? "running" : t?.dur !== undefined ? fmtSpan(t.dur) : s.status === "skipped" ? "skipped" : s.status === "failed" ? "failed" : state === "stopped" ? "stopped" : ""}</span>
                </li>
              );
            })}
          </ul>
        </> : <div className="ins-empty">{Object.values(run.pending).some((p) => p.kind === "plan_review")
          ? "A plan is waiting for your review." : "The worker hasn't written a plan yet."}</div>}
      </div>
    </section>

    <section className="ins-sec ins-last">
      <div className="ins-h">
        <h3>Working memory</h3>
        <div className="right"><span className="num" id={`${prefix}factCount`}>{facts.length ? `${facts.length} ${facts.length === 1 ? "fact" : "facts"}` : ""}</span></div>
      </div>
      <dl className="ins-mem" id={`${prefix}facts`}>
        {facts.length ? facts.map(([k, v]) => (
          <div key={k} className={cx(k === run.freshFact && running && "fresh")}><dt>{k.replace(/_/g, " ")}</dt><dd>{v}</dd></div>
        )) : <div className="ins-empty">Facts the worker extracts (amounts, dates, IDs) land here.</div>}
      </dl>
    </section>
  </>;
}
