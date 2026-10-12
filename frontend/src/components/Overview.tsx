import { useState } from "react";
import { useStored } from "../hooks/useApp";
import { outcomeOf } from "../lib/constants";
import { ago, fmtDur, fmtNum } from "../lib/format";
import type { Outcome, Quota, RunSummary } from "../lib/types";
import { ColumnsTable, Legend, OutcomeMix, StackedColumns, type Series } from "./Charts";
import { Icon } from "./Icon";
import "./home.css";

// Outcomes are states, so they use the status palette (with icons), never categorical series colours.
const GROUPS: (Series & { outcomes: Outcome[] })[] = [
  { key: "verified", label: "Verified", color: "var(--ok-fill)", icon: "shield", tone: "ok", outcomes: ["completed_verified"] },
  { key: "attention", label: "Needs attention", color: "var(--warn-fill)", icon: "alert", tone: "warn", outcomes: ["completed_unverified", "blocked", "cancelled"] },
  { key: "failed", label: "Failed", color: "var(--err-fill)", icon: "x", tone: "err", outcomes: ["failed", "error"] },
];
const groupOf = (o: Outcome) => GROUPS.find((g) => g.outcomes.includes(o))?.key ?? "attention";

const DAY = 86400;
const RANGES = [7, 14, 30] as const;
const dayKey = (t: number) => new Date(t * 1000).toDateString();

function lastDays(n: number) {
  const out: Date[] = [];
  const now = new Date();
  for (let i = n - 1; i >= 0; i--) out.push(new Date(now.getFullYear(), now.getMonth(), now.getDate() - i));
  return out;
}

/** Clock-style duration for dense mono columns: 1:34, 12:05, 1:02:10. */
function clock(s: number) {
  const t = Math.round(s), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), r = String(t % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${m}:${r}`;
}

function started(t: number) {
  const d = new Date(t * 1000), now = new Date();
  const hm = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  if (d.toDateString() === now.toDateString()) return hm;
  if (Date.now() / 1000 - t < 6 * DAY) return `${d.toLocaleDateString(undefined, { weekday: "short" })} ${hm}`;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

const Chev = () => <svg className="i i-12" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>;
const Arrow = ({ up }: { up: boolean }) => (
  <svg className="i i-12" viewBox="0 0 24 24" aria-hidden="true"><path d={up ? "M12 19V5M5 12l7-7 7 7" : "M12 5v14M19 12l-7 7-7-7"} /></svg>
);
export const Hand = ({ className = "i i-14" }: { className?: string }) => (
  <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
    <path d="M18 11V6a2 2 0 0 0-4 0v0M14 10V4a2 2 0 0 0-4 0v2M10 10.5V6a2 2 0 0 0-4 0v8" />
    <path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15" />
  </svg>
);

/** "▼ 7% vs prior week (15)". Colour follows good/bad, not direction (`goodUp`). */
function Delta({ now, before, goodUp = true, fmt = (n: number) => `${Math.abs(n)}%` }:
  { now: number; before: number; goodUp?: boolean; fmt?: (n: number) => string }) {
  if (!before) return null;
  const pct = Math.round(((now - before) / before) * 100);
  if (!pct) return <span className="delta">0%</span>;
  const good = (pct > 0) === goodUp;
  return <span className={`delta ${good ? "up" : "down"}`}><Arrow up={pct > 0} />{fmt(pct)}</span>;
}

interface Props { runs: RunSummary[]; quota: Quota | null; onOpen: (id: string) => void; onNew: () => void }

/** Operations overview: KPI strip, runs per day, outcome mix, recent runs. */
export function Overview({ runs, quota, onOpen, onNew }: Props) {
  const [rangeS, setRange] = useStored("ovRange", "14");
  const [view, setView] = useState<"chart" | "table">("chart");
  const range = (RANGES as readonly number[]).includes(+rangeS) ? +rangeS : 14;

  const now = Date.now() / 1000;
  const days = lastDays(range);
  const from = days[0].getTime() / 1000;
  const inRange = runs.filter((r) => r.created >= from);
  const finished = inRange.filter((r) => r.outcome);
  const verified = finished.filter((r) => r.outcome === "completed_verified");
  const counts = Object.fromEntries(GROUPS.map((g) => [g.key, finished.filter((r) => groupOf(r.outcome!) === g.key).length]));
  const vPct = finished.length ? (100 * verified.length) / finished.length : 0;

  const week = runs.filter((r) => r.created > now - 7 * DAY);
  const prior = runs.filter((r) => r.created <= now - 7 * DAY && r.created > now - 14 * DAY);
  const durs = (rs: RunSummary[]) => rs.filter((r) => r.outcome && r.duration_s).map((r) => r.duration_s!).sort((a, b) => a - b);
  const avg = (xs: number[]) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : 0);
  const dRange = durs(inRange), avgDur = avg(dRange), p90 = dRange.length ? dRange[Math.min(dRange.length - 1, Math.floor(dRange.length * 0.9))] : 0;
  const avgWeek = avg(durs(week)), avgPrior = avg(durs(prior));
  const waiting = runs.filter((r) => r.pending.length);
  const running = runs.filter((r) => !r.outcome);
  const stepsList = finished.filter((r) => r.steps).map((r) => r.steps!);
  const tokens = finished.reduce((s, r) => s + (r.tokens ?? 0), 0);

  const columns = days.map((d) => {
    const rs = finished.filter((r) => dayKey(r.created) === d.toDateString());
    const values: Record<string, number> = {};
    for (const g of GROUPS) values[g.key] = rs.filter((r) => groupOf(r.outcome!) === g.key).length;
    return {
      label: d.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
      full: d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }),
      values,
    };
  });
  const span = `${days[0].toLocaleDateString(undefined, { month: "short", day: "numeric" })} – ${days[days.length - 1].toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`;

  const q = quota?.models.find((m) => m.model === quota.active) ?? quota?.models[0];
  const limit = q?.daily_limit;

  return (
    <div className="home-view">

      <div className="page overview">
        <div className="page-h">
          <h1>Overview</h1>
          <span className="sub">{span}</span>
          {running.length > 0 && <span className="sub live-n"><span className="dot live" />{running.length} running now</span>}
          <div className="right">
            <div className="seg" role="group" aria-label="Range">
              {RANGES.map((r) => (
                <button key={r} className={r === range ? "on" : ""} aria-pressed={r === range} onClick={() => setRange(String(r))}>{r}d</button>
              ))}
            </div>
          </div>
        </div>

        <div className="stats" id="kpis">
          <div className="stat">
            <span className="k">Verified completions</span>
            <span className="v">{verified.length}<small>/{finished.length}</small></span>
            <span className="d">{finished.length ? <><span className="delta up">{Math.round(vPct)}%</span>passed independent audit</> : "No finished runs yet"}</span>
            <span className="bar" aria-hidden="true">
              {finished.length
                ? GROUPS.filter((g) => counts[g.key]).map((g) => <i key={g.key} style={{ flex: counts[g.key], background: g.color }} />)
                : <i className="track" style={{ flex: 1 }} />}
            </span>
          </div>
          <div className="stat">
            <span className="k">Runs this week</span>
            <span className="v">{week.length}</span>
            <span className="d"><Delta now={week.length} before={prior.length} />{prior.length ? `vs prior week (${prior.length})` : "no runs the week before"}</span>
          </div>
          <div className="stat">
            <span className="k">Avg time per task</span>
            <span className="v">{avgDur ? fmtDur(avgDur) : "–"}</span>
            <span className="d">
              {avgWeek && avgPrior ? <span className={`delta ${avgWeek <= avgPrior ? "up" : "down"}`}><Arrow up={avgWeek > avgPrior} />{fmtDur(Math.abs(avgWeek - avgPrior))}</span> : null}
              {avgWeek && avgPrior ? (avgWeek <= avgPrior ? "faster" : "slower") : `${dRange.length} timed run${dRange.length === 1 ? "" : "s"}`}
              {p90 ? ` · p90 ${fmtDur(p90)}` : ""}
            </span>
          </div>
          <div className={waiting.length ? "stat attn" : "stat"}>
            <span className="k"><Hand className={waiting.length ? "i i-14 warn-i" : "i i-14"} />Waiting for you</span>
            <span className="v">{waiting.length}</span>
            <span className="d">
              {waiting.length
                ? <button className="lnk" onClick={() => onOpen(waiting[0].run_id)}>Review {waiting.length > 1 ? "first request" : "request"}<Chev /></button>
                : "Nothing needs a decision"}
            </span>
          </div>
          <div className="stat">
            <span className="k">Model requests today</span>
            <span className="v">{q ? q.requests : "–"}{limit ? <small>/{limit}</small> : null}</span>
            <span className="d">
              {q ? (limit ? `${Math.max(0, limit - q.requests)} left` : q.model) : "No quota data"}
              {q?.resets_in ? ` · resets in ${q.resets_in}` : ""}
            </span>
            {q && limit ? (
              <span className="bar" aria-hidden="true">
                <i style={{ flex: Math.min(q.requests, limit), background: q.exhausted ? "var(--err-fill)" : "var(--accent)" }} />
                <i className="track" style={{ flex: Math.max(0, limit - q.requests) }} />
              </span>
            ) : null}
          </div>
        </div>

        <div className="grid-2">
          <div className="card">
            <div className="card-h">
              <h3>Runs per day</h3>
              <Legend series={GROUPS} />
              <div className="seg sm" role="group" aria-label="View as">
                <button className={view === "chart" ? "on" : ""} aria-pressed={view === "chart"} onClick={() => setView("chart")}>Chart</button>
                <button className={view === "table" ? "on" : ""} aria-pressed={view === "table"} onClick={() => setView("table")}>Table</button>
              </div>
            </div>
            {view === "chart"
              ? <div className="chart-wrap"><StackedColumns columns={columns} series={GROUPS} /></div>
              : <ColumnsTable columns={columns} series={GROUPS} />}
          </div>
          <div className="card">
            <div className="card-h"><h3>Outcome mix</h3><span className="muted card-note">{finished.length ? `Last ${finished.length} run${finished.length === 1 ? "" : "s"}` : "No runs yet"}</span></div>
            <div className="mix">
              <div className="big"><b>{finished.length ? `${vPct.toFixed(1)}%` : "–"}</b><span className="muted">verified on audit</span></div>
              <OutcomeMix parts={GROUPS.map((g) => ({ ...g, value: counts[g.key] }))} />
              <div className="mixfoot">
                <div><span className="muted">Avg steps per run</span><b>{stepsList.length ? (stepsList.reduce((s, x) => s + x, 0) / stepsList.length).toFixed(1) : "–"}</b></div>
                <div><span className="muted">Tokens used</span><b>{tokens ? fmtNum(tokens) : "–"}</b></div>
              </div>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-h"><h3>Recent runs</h3>{runs.length > 0 && <span className="muted card-note">{runs.length} total</span>}</div>
          {runs.length ? (
            <div className="tbl-wrap">
              <table className="tbl runs-tbl">
                <thead><tr>
                  <th>Task</th><th>Status</th><th className="r hide-sm">Steps</th><th className="r">Duration</th>
                  <th className="r hide-sm">Tokens</th><th className="r hide-sm">Started</th>
                </tr></thead>
                <tbody>
                  {runs.slice(0, 12).map((r) => {
                    const o = r.outcome ? outcomeOf(r.outcome) : null;
                    const g = r.outcome ? GROUPS.find((x) => x.key === groupOf(r.outcome!))! : null;
                    return (
                      <tr key={r.run_id} tabIndex={0} onClick={() => onOpen(r.run_id)}
                        onKeyDown={(e) => { if (e.key === "Enter") onOpen(r.run_id); }}>
                        <td className="task"><b title={r.task}>{r.task}</b><span>{r.run_id}</span></td>
                        <td>{o && g ? <span className={`pill ${g.tone}`} title={o.label}><Icon name={g.icon} />{g.label}</span>
                          : r.pending.length ? <span className="pill warn"><Hand className="i" />Waiting for you</span>
                          : <span className="pill live"><span className="dot live" />Running</span>}</td>
                        <td className="r mono hide-sm">{r.steps ?? "–"}</td>
                        <td className="r mono">{r.duration_s ? clock(r.duration_s) : "–"}</td>
                        <td className="r mono hide-sm">{r.tokens ? fmtNum(r.tokens) : "–"}</td>
                        <td className="r muted hide-sm" title={ago(r.created)}>{started(r.created)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-s">
              <span>No runs yet. Describe a task and the worker takes it from there.</span>
              <button className="btn sm" onClick={onNew}><Icon name="plus" />New task</button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
