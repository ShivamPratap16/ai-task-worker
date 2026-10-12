import { useNow } from "../hooks/useApp";
import { outcomeOf } from "../lib/constants";
import { cx, fmtDur, fmtNum } from "../lib/format";
import type { RunState } from "../lib/runState";
import { Icon } from "./Icon";
import { XIcon } from "./Timeline";

/** Task title plus the fact strip: Status · Model · Elapsed · Steps · Tokens · Plan.
 *  (Stop / Run again live in the shell's top bar.) */
export function RunHeader({ run }: { run: RunState }) {
  const f = run.finished;
  const live = !f && !run.streamEnded;
  const now = useNow(live);
  const waiting = Object.keys(run.pending).length > 0;
  const o = f ? outcomeOf(f.outcome) : null;
  const elapsed = f?.duration_s ?? (run.t0 ? now - run.t0 : 0);
  const steps = f?.steps ?? run.steps;
  const total = run.plan.length;
  const done = run.plan.filter((s) => s.status === "done" || s.status === "skipped").length;
  const u = run.usage;
  const model = run.model.includes("/") ? run.model.split("/").slice(1).join("/") : run.model;

  return (
    <header className="run-head" id="head">
      <h1>{run.task || "Loading…"}</h1>
      <div className="facts">
        <div className="fact f-status">
          <span className="k">Status</span>
          <span className="v">
            {o ? <span className={cx("pill", o.tone !== "none" && o.tone)}><Icon name={o.icon} />{o.label}</span>
              : waiting ? <span className="pill warn"><XIcon name="hand" />Waiting for you</span>
              : live ? <span className="pill live"><span className="dot live" />Running</span>
              : <span className="pill"><Icon name="stop" />Stopped</span>}
          </span>
        </div>
        <div className="fact f-model hide-sm">
          <span className="k">Model</span>
          <span className="v mono" title={run.model}>{model || "—"}</span>
        </div>
        <div className="fact f-elapsed"><span className="k">Elapsed</span><span className="v mono">{fmtDur(elapsed)}</span></div>
        <div className="fact"><span className="k">Steps</span><span className="v mono">{steps}</span></div>
        <div className="fact">
          <span className="k">Tokens</span>
          <span className="v mono" title={u ? `input ${u.input_tokens.toLocaleString()} · output ${u.output_tokens.toLocaleString()} · cached ${u.cache_read_tokens.toLocaleString()}` : undefined}>
            {u ? fmtNum(u.input_tokens + u.output_tokens) : "—"}
          </span>
        </div>
        <div className="fact grow f-plan">
          <span className="k">Plan</span>
          <span className="v">
            {total ? <>
              <span className={cx("progress", done === total && "ok")} role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done} aria-label="Plan progress">
                <i style={{ width: `${(100 * done) / total}%` }} />
              </span>
              <span className="num">{done} of {total}</span>
            </> : <span className="faint">No plan yet</span>}
          </span>
        </div>
      </div>
    </header>
  );
}
