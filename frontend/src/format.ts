export const fmtNum = (v: number | null | undefined) => (v == null ? "–" : v.toLocaleString("en-US"));
export const fmt3 = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? "–" : v.toFixed(3));
export const fmtCost = (v: number | null | undefined) => (v == null ? "–" : `$${v.toFixed(2)}`);
export const fmtPct = (v: number | null | undefined) => (v == null ? "–" : `${Math.round(v * 100)}%`);

/** Server times are UTC; treat a time without an offset as UTC too. */
export const parseTime = (iso: string) => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "–";
  return parseTime(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function fmtDuration(start: string | null, end: string | null): string {
  if (!start) return "–";
  const ms = (end ? parseTime(end) : new Date()).getTime() - parseTime(start).getTime();
  const m = Math.max(0, Math.round(ms / 60000));
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}
