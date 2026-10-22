import type { Metrics } from "../api/types";

const W = 640;
const H = 220;
const PAD = { l: 36, r: 10, t: 14, b: 24 };

function scaleX(lo: number, hi: number) {
  return (v: number) => PAD.l + ((v - lo) / (hi - lo || 1)) * (W - PAD.l - PAD.r);
}

/** Score histogram of found papers against the random baseline, with the three thresholds. */
export function ScoreHistogram({ m }: { m: Metrics }) {
  const h = m.hist;
  if (!h || h.edges.length < 2) return <p className="muted">The distribution appears after the first papers are scored.</p>;
  const lo = h.edges[0]!;
  const hi = h.edges[h.edges.length - 1]!;
  const x = scaleX(lo, hi);
  const total = (a: number[]) => a.reduce((s, v) => s + v, 0) || 1;
  const so = total(h.observed);
  const sn = total(h.null);
  const dens = (v: number, s: number) => v / s;
  const max = Math.max(...h.observed.map((v) => dens(v, so)), ...h.null.map((v) => dens(v, sn)), 1e-9);
  const y = (v: number) => H - PAD.b - (v / max) * (H - PAD.t - PAD.b);
  const bars = (arr: number[], s: number, color: string, opacity: number) =>
    arr.map((v, i) => {
      const x0 = x(h.edges[i]!);
      const x1 = x(h.edges[i + 1]!);
      return <rect key={i} x={x0} y={y(dens(v, s))} width={Math.max(x1 - x0 - 1, 0.5)} height={H - PAD.b - y(dens(v, s))} fill={color} opacity={opacity} />;
    });
  const t = m.thresholds;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => lo + (hi - lo) * f);
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Score distribution of found papers and random papers">
      {bars(h.null, sn, "var(--chart-null)", 0.5)}
      {bars(h.observed, so, "var(--chart-obs)", 0.6)}
      {t &&
        ([["drop", t.drop], ["rel", t.rel], ["core", t.core]] as const).map(([name, v], i) => (
          <g key={name}>
            <line x1={x(v)} x2={x(v)} y1={PAD.t} y2={H - PAD.b} stroke="var(--chart-mark)" strokeDasharray="4 3" />
            <text x={x(v) + 3} y={PAD.t + 10 + i * 12} style={{ fill: "var(--chart-mark)" }}>
              {name} {v.toFixed(3)}
            </text>
          </g>
        ))}
      <line x1={PAD.l} x2={W - PAD.r} y1={H - PAD.b} y2={H - PAD.b} stroke="var(--line)" />
      {ticks.map((v) => (
        <text key={v} x={x(v)} y={H - 8} textAnchor="middle">
          {v.toFixed(2)}
        </text>
      ))}
    </svg>
  );
}

/** LLM judgments (dots) against the score, with the fitted probability curve. */
export function CalibrationChart({ m }: { m: Metrics }) {
  const c = m.calibration;
  const grid = m.mixture?.grid;
  if (!c || !c.points.length || !grid || grid.length < 2) return <p className="muted">No LLM judgments yet.</p>;
  const lo = grid[0]!;
  const hi = grid[grid.length - 1]!;
  const x = scaleX(lo, hi);
  const y = (p: number) => H - PAD.b - p * (H - PAD.t - PAD.b);
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="LLM judgments against score">
      {c.points.map(([v, r], i) => (
        <circle key={i} cx={x(v)} cy={y(r ? 0.96 - (i % 5) * 0.02 : 0.04 + (i % 5) * 0.02)} r={2.6}
          fill={r ? "var(--good)" : "var(--chart-null)"} opacity={0.75} />
      ))}
      {c.curve.length > 0 && (
        <polyline fill="none" stroke="var(--ink)" strokeWidth={2} points={grid.map((g, i) => `${x(g)},${y(c.curve[i] ?? 0)}`).join(" ")} />
      )}
      <line x1={PAD.l} x2={W - PAD.r} y1={H - PAD.b} y2={H - PAD.b} stroke="var(--line)" />
      <text x={PAD.l} y={PAD.t}>{`${c.labels} judged · ${c.relevant} relevant`}</text>
      {[0, 0.5, 1].map((f) => (
        <text key={f} x={x(lo + (hi - lo) * f)} y={H - 8} textAnchor="middle">
          {(lo + (hi - lo) * f).toFixed(2)}
        </text>
      ))}
    </svg>
  );
}
