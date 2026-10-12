import { outcomeOf } from "../lib/constants";
import { cx } from "../lib/format";
import type { Quota, RunSummary } from "../lib/types";
import { Icon } from "./Icon";

function status(r: RunSummary): [string, string] {
  if (r.outcome) {
    const o = outcomeOf(r.outcome);
    return [o.tone === "none" ? "" : o.tone, o.label];
  }
  if (r.pending.length) return ["warn", "Waiting for you"];
  return ["live", "Running"];
}

export function quotaLine(quota: Quota | null): string {
  if (!quota?.models.length) return "";
  const primary = quota.models[0];
  const active = quota.models.find((m) => m.model === quota.active);
  const shown = active ?? primary;
  return `${shown.requests || 0} requests today${shown.daily_limit ? ` of ${shown.daily_limit}` : ""}` +
    (active && active !== primary ? ` · using ${active.model}` : "");
}

/** Share of today's request budget used by the active model, or null when the provider has no daily limit. */
function quotaUse(quota: Quota | null): number | null {
  const shown = quota?.models.find((m) => m.model === quota.active) ?? quota?.models[0];
  if (!shown?.daily_limit) return null;
  return Math.min(1, (shown.requests || 0) / shown.daily_limit);
}

/** "10:42" for today, "Fri" within the last week, "Oct 3" before that. */
function when(created: number): string {
  const d = new Date(created * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  if (now.getTime() - d.getTime() < 6 * 86400_000) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function Logo() {
  return <div className="logo" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M4 7h16" /><path d="M12 7v13" /><path d="M8 20h8" /></svg></div>;
}

interface Props {
  runs: RunSummary[];
  current: string | null;
  provider: string;
  quota: Quota | null;
  open: boolean;
  dark: boolean;
  page: "new" | "overview" | "run";
  onOpen: (id: string) => void;
  onNew: () => void;
  onOverview: () => void;
  onToggleTheme: () => void;
}

export function Sidebar({ runs, current, provider, quota, open, dark, page, onOpen, onNew, onOverview, onToggleTheme }: Props) {
  const waitingRuns = runs.filter((r) => r.pending.length);
  const today = new Date().toDateString();
  const groups: [string, RunSummary[]][] = [
    ["Today", runs.filter((r) => new Date(r.created * 1000).toDateString() === today)],
    ["Earlier", runs.filter((r) => new Date(r.created * 1000).toDateString() !== today)],
  ];
  const use = quotaUse(quota);
  const goWaiting = () => (waitingRuns.length ? onOpen(waitingRuns[0].run_id) : onOverview());

  return (
    <aside className={cx("sidebar", open && "open")} id="sidebar" aria-label="Primary">
      <div className="ws">
        <Logo />
        <div className="ws-txt"><b>Task Worker</b><span>Acme Corp sandbox</span></div>
      </div>
      <div className="side-pad">
        <button className="btn primary newtask" id="newBtn" onClick={onNew} aria-current={page === "new" ? "page" : undefined}>
          <Icon name="plus" />New task<kbd>N</kbd>
        </button>
      </div>
      <nav className="nav">
        <button className="nav-item" id="navOverview" onClick={onOverview} aria-current={page === "overview" ? "page" : undefined}>
          <Icon name="dash" />Overview
        </button>
        <button className="nav-item" id="navWaiting" onClick={goWaiting} title={waitingRuns.length ? "Open the oldest run that needs you" : "Nothing is waiting for you"}>
          <Icon name="hand" />Waiting for you
          <span className={cx("count", waitingRuns.length > 0 && "attn")}>{waitingRuns.length}</span>
        </button>
      </nav>
      <div className="side-scroll" id="runs">
        {!runs.length && <div className="side-empty">No runs yet. Your task history will show up here.</div>}
        {groups.filter(([, rs]) => rs.length).map(([name, rs]) => (
          <div key={name}>
            <div className="side-group"><span>{name}</span></div>
            <div className="runlist">
              {rs.map((r) => {
                const [tone, label] = status(r);
                const active = r.run_id === current;
                return (
                  <a key={r.run_id} href={`#${encodeURIComponent(r.run_id)}`} className={cx("runlink run-item", active && "active")}
                    aria-current={active ? "page" : undefined} title={`${r.task}\n${label}`}
                    onClick={(e) => { e.preventDefault(); onOpen(r.run_id); }}>
                    <span className={cx("dot", tone)} aria-hidden="true" />
                    <span className="t">{r.task}</span>
                    <span className="when">{when(r.created)}</span>
                    <span className="sr">{label}</span>
                  </a>
                );
              })}
            </div>
          </div>
        ))}
      </div>
      <div className="side-foot">
        <div className="model"><span className="mono" id="model" title={provider}>{provider || "connecting…"}</span></div>
        <div className="foot-row">
          <div className="quota">
            <span id="quotaLine" title={quotaLine(quota)}>{quotaLine(quota)}</span>
            {use !== null && <span className={cx("meter", use > .9 ? "err" : use > .7 && "warn")} aria-hidden="true"><i style={{ width: `${Math.max(use * 100, 3)}%` }} /></span>}
          </div>
          <button className="icon-btn" id="themeBtn" title="Toggle theme" aria-label={dark ? "Switch to light theme" : "Switch to dark theme"} onClick={onToggleTheme}>
            <Icon name={dark ? "moon" : "sun"} />
          </button>
        </div>
      </div>
    </aside>
  );
}
