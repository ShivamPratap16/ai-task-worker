// Small hand-built SVG charts. Conventions (data-viz guide + design concept B): thin marks, a 2px surface gap
// between stacked fills, small rounded data-ends with the baseline flat, recessive grid and axes, mono tabular
// tick labels, a hover tooltip per column, a table view, text in ink tokens (never the series color), and status
// colours always paired with an icon + label.
import { useEffect, useRef, useState } from "react";
import { Icon, type IconName } from "./Icon";

export interface Series { key: string; label: string; color: string; icon: IconName; tone: "ok" | "warn" | "err" }
export interface Column { label: string; full: string; values: Record<string, number> }

/** Status label: icon + text in the status ink colour (never colour alone). */
export function StatusLabel({ s }: { s: Series }) {
  return <span className={`st ${s.tone}`}><Icon name={s.icon} />{s.label}</span>;
}

/** Legend for the stacked chart: swatch + label, sits in the card header. */
export function Legend({ series }: { series: Series[] }) {
  return (
    <div className="legend">
      {series.map((s) => <span key={s.key}><i style={{ background: s.color }} />{s.label}</span>)}
    </div>
  );
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [w, setW] = useState(600);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(200, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

/** Integer ticks 0..top with at most ~5 steps. */
function niceTicks(max: number) {
  const step = max <= 5 ? 1 : max <= 10 ? 2 : Math.ceil(max / 5 / 5) * 5;
  const top = Math.max(step, Math.ceil(max / step) * step);
  const out: number[] = [];
  for (let t = 0; t <= top; t += step) out.push(t);
  return { ticks: out, top };
}

/** Rect with only the top corners rounded (data-end); the baseline side stays flat. */
function roundedTop(x: number, y: number, w: number, h: number, r: number) {
  r = Math.min(r, h, w / 2);
  if (r <= 0) return `M${x},${y}h${w}v${h}h${-w}Z`;
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

/** Stacked columns over time with a per-column hover tooltip. */
export function StackedColumns({ columns, series }: { columns: Column[]; series: Series[] }) {
  const [wrap, W] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null);
  const totals = columns.map((c) => series.reduce((s, k) => s + (c.values[k.key] ?? 0), 0));
  const { ticks, top } = niceTicks(Math.max(1, ...totals));
  const H = 248, L = 24, R = 4, T = 8, B = 24;
  const plotH = H - T - B;
  const cw = (W - L - R) / Math.max(1, columns.length);
  const bw = Math.max(3, Math.min(14, cw * 0.36));
  const y = (v: number) => T + plotH * (1 - v / top);
  const every = Math.max(1, Math.ceil(columns.length / Math.max(2, Math.floor((W - L) / 64))));

  return (
    <div className="chart-plot" ref={wrap} onPointerLeave={() => setHover(null)}>
      <svg className="chart-svg" width="100%" height={H} viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label={`Stacked columns of runs per day over ${columns.length} days`}>
        {ticks.map((t) => (
          <g key={t}>
            <line className={t ? "grid" : "base"} x1={L} x2={W - R} y1={y(t)} y2={y(t)} />
            <text x={L - 8} y={y(t) + 3.5} textAnchor="end">{t}</text>
          </g>
        ))}
        {columns.map((c, i) => {
          const x = L + cw * i + (cw - bw) / 2;
          const present = series.filter((s) => (c.values[s.key] ?? 0) > 0);
          let acc = 0;
          return (
            <g key={c.full} className={hover?.i === i ? "col on" : "col"}
              onPointerMove={(e) => {
                const r = wrap.current!.getBoundingClientRect();
                setHover({ i, x: e.clientX - r.left, y: e.clientY - r.top });
              }}>
              <rect x={L + cw * i} y={T} width={cw} height={plotH} fill="transparent" />
              {present.map((s, j) => {
                const v = c.values[s.key];
                const y1 = y(acc + v), y0 = y(acc);
                const gap = acc > 0 ? 2 : 0;
                acc += v;
                const h = Math.max(0, y0 - y1 - gap);
                return <path key={s.key} fill={s.color} d={roundedTop(x, y1, bw, h, j === present.length - 1 ? 2 : 0)} />;
              })}
              {(columns.length - 1 - i) % every === 0 && <text x={L + cw * i + cw / 2} y={H - 6} textAnchor="middle">{c.label}</text>}
            </g>
          );
        })}
      </svg>
      {!totals.some(Boolean) && <div className="chart-empty">No finished runs in this period</div>}
      {hover && totals.some(Boolean) && (
        <div className="tip" style={{
          left: hover.x + 12 > W - 184 ? hover.x - 196 : hover.x + 12,
          top: Math.max(0, hover.y - 10),
        }}>
          <div className="t">{columns[hover.i].full} · {totals[hover.i]} run{totals[hover.i] === 1 ? "" : "s"}</div>
          {series.map((s) => (
            <div key={s.key} className="r"><i style={{ background: s.color }} />{s.label}<b>{columns[hover.i].values[s.key] ?? 0}</b></div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Same data as StackedColumns, as a table (the accessible view). */
export function ColumnsTable({ columns, series }: { columns: Column[]; series: Series[] }) {
  return (
    <div className="tbl-wrap chart-table">
      <table className="tbl compact">
        <thead><tr><th>Day</th>{series.map((s) => <th key={s.key} className="r">{s.label}</th>)}<th className="r">Total</th></tr></thead>
        <tbody>
          {columns.slice().reverse().map((c) => (
            <tr key={c.full}>
              <td>{c.full}</td>
              {series.map((s) => <td key={s.key} className="r mono">{c.values[s.key] ?? 0}</td>)}
              <td className="r mono">{series.reduce((n, s) => n + (c.values[s.key] ?? 0), 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Part-to-whole as one thin horizontal bar (2px gaps) plus status rows with count and share. */
export function OutcomeMix({ parts }: { parts: (Series & { value: number })[] }) {
  const [hover, setHover] = useState<string | null>(null);
  const total = parts.reduce((s, p) => s + p.value, 0);
  const pct = (v: number) => (total ? `${Math.round((100 * v) / total)}%` : "–");
  return (
    <>
      <div className="mixbar" aria-hidden="true" onPointerLeave={() => setHover(null)}>
        {total === 0 ? <i className="empty" style={{ flex: 1 }} /> : parts.filter((p) => p.value > 0).map((p) => (
          <i key={p.key} style={{ flex: p.value, background: p.color }} className={hover && hover !== p.key ? "dim" : ""}
            title={`${p.label}: ${p.value} (${pct(p.value)})`} onPointerEnter={() => setHover(p.key)} />
        ))}
      </div>
      <div className="mixrows">
        {parts.map((p) => (
          <div key={p.key} className={hover === p.key ? "mixrow on" : "mixrow"}
            onPointerEnter={() => setHover(p.key)} onPointerLeave={() => setHover(null)}>
            <StatusLabel s={p} /><span className="n">{p.value}</span><span className="p">{pct(p.value)}</span>
          </div>
        ))}
      </div>
    </>
  );
}
