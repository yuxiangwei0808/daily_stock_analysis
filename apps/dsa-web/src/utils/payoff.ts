import type { PayoffPoint } from '../types/tradeDesk';

/**
 * Window the expiry payoff around strikes and break-evens. Server points span
 * $0 to ~2x the price, which squeezes a narrow spread into a single step.
 * The payoff is piecewise linear, so cutting by interpolation is exact.
 */
export function focusPayoffPoints(points: PayoffPoint[], anchors: number[]): PayoffPoint[] {
  const sorted = points.filter((p) => Number.isFinite(p.price) && Number.isFinite(p.pnl)).sort((a, b) => a.price - b.price);
  const marks = anchors.filter((value) => Number.isFinite(value) && value > 0);
  if (sorted.length < 2 || !marks.length) return sorted;
  const low = Math.min(...marks);
  const high = Math.max(...marks);
  const pad = Math.max((high - low) * 0.5, high * 0.08);
  const from = Math.max(sorted[0].price, low - pad);
  const to = Math.min(sorted[sorted.length - 1].price, high + pad);
  const at = (x: number) => {
    const i = Math.max(1, sorted.findIndex((p) => p.price >= x));
    const [a, b] = [sorted[i - 1], sorted[Math.min(i, sorted.length - 1)]];
    return b.price === a.price ? a.pnl : a.pnl + ((b.pnl - a.pnl) * (x - a.price)) / (b.price - a.price);
  };
  return [{ price: from, pnl: at(from) }, ...sorted.filter((p) => p.price > from && p.price < to), { price: to, pnl: at(to) }];
}

const sortedPoints = (points: PayoffPoint[]) =>
  points.filter((p) => Number.isFinite(p.price) && Number.isFinite(p.pnl)).sort((a, b) => a.price - b.price);

/**
 * Expiry P/L at any underlying price. Exact: the payoff is linear between the
 * server's points (0, each strike, and far-out tail samples); beyond the last
 * point the last segment's slope continues.
 */
export function pnlAt(points: PayoffPoint[], price: number): number {
  const sorted = sortedPoints(points);
  if (!sorted.length) return Number.NaN;
  if (sorted.length === 1) return sorted[0].pnl;
  let i = sorted.findIndex((p) => p.price >= price);
  if (i === -1) i = sorted.length - 1;
  i = Math.max(1, i);
  const [a, b] = [sorted[i - 1], sorted[i]];
  return b.price === a.price ? b.pnl : a.pnl + ((b.pnl - a.pnl) * (price - a.price)) / (b.price - a.price);
}

/** Evenly spaced samples across [from, to] plus the exact kinks and marks, so hovering reads any price. */
export function samplePayoff(points: PayoffPoint[], from: number, to: number, marks: number[] = [], count = 160): PayoffPoint[] {
  if (!(to > from)) return [];
  const xs = new Set<number>();
  for (let i = 0; i < count; i += 1) xs.add(from + ((to - from) * i) / (count - 1));
  for (const x of [...sortedPoints(points).map((p) => p.price), ...marks]) if (x > from && x < to) xs.add(x);
  return [...xs].sort((a, b) => a - b).map((price) => ({ price, pnl: pnlAt(points, price) }));
}

export interface PayoffRow {
  price: number;
  label: string;
  /** Move from the current price, in percent; null without a current price. */
  movePct: number | null;
  pnl: number;
  kind: 'now' | 'move' | 'breakeven' | 'strike';
}

/**
 * P/L at expiry for the prices a trader asks about: the current price, moves of
 * ±5/10/20% from it, each breakeven and each strike, in price order. A move that
 * lands within 0.5% of a key level gives way to the level.
 */
export function payoffTable(points: PayoffPoint[], spot: number | null | undefined, breakevens: number[], strikes: number[]): PayoffRow[] {
  const now = spot != null && Number.isFinite(spot) && spot > 0 ? spot : null;
  const move = (price: number) => (now ? (price / now - 1) * 100 : null);
  const keys: PayoffRow[] = [
    ...(now ? [{ price: now, label: 'Now', kind: 'now' as const }] : []),
    ...breakevens.filter((x) => Number.isFinite(x) && x > 0).map((price) => ({ price, label: 'Breakeven', kind: 'breakeven' as const })),
    ...[...new Set(strikes.filter((x) => Number.isFinite(x) && x > 0))].map((price) => ({ price, label: 'Strike', kind: 'strike' as const })),
  ].map((row) => ({ ...row, movePct: move(row.price), pnl: pnlAt(points, row.price) }));
  const near = (a: number, b: number) => Math.abs(a - b) <= Math.max(Math.abs(b) * 0.005, 0.005);
  const moves: PayoffRow[] = now
    ? [-20, -10, -5, 5, 10, 20].map((pct) => now * (1 + pct / 100))
        .filter((price) => !keys.some((row) => near(row.price, price)))
        .map((price) => ({ price, label: '', movePct: move(price), pnl: pnlAt(points, price), kind: 'move' as const }))
    : [];
  const unique: PayoffRow[] = [];
  for (const row of keys) {
    const same = unique.find((kept) => near(kept.price, row.price));
    if (same) same.label = [...new Set([same.label, row.label])].join(' · ');
    else unique.push(row);
  }
  return [...unique, ...moves].sort((a, b) => a.price - b.price);
}

/**
 * Where a flat extreme (max gain or max loss) holds: "at or below 35.00", "at or above 40.00",
 * "between 30.00 and 38.00", or both tails of an iron condor ("at or below 28.00 or at or above 40.00").
 */
export function extremeRange(points: PayoffPoint[], value: number, unboundedAbove: boolean): string {
  const sorted = sortedPoints(points);
  if (!sorted.length || !Number.isFinite(value)) return '';
  const hit = (p: PayoffPoint) => Math.abs(p.pnl - value) <= Math.max(Math.abs(value) * 1e-6, 0.01);
  // Neighbouring points at the extreme are flat between them (the payoff is linear between points).
  const runs: Array<[number, number]> = [];
  sorted.forEach((p, i) => {
    if (!hit(p)) return;
    if (i > 0 && hit(sorted[i - 1]) && runs.length) runs[runs.length - 1][1] = p.price;
    else runs.push([p.price, p.price]);
  });
  if (!runs.length) return '';
  const fmt = (x: number) => x.toFixed(2);
  const top = sorted[sorted.length - 1].price;
  const parts = runs.map(([low, high]) => {
    const fromZero = low <= 0;
    const toTop = high >= top && !unboundedAbove;
    if (fromZero && toTop) return 'at any price';
    if (fromZero) return `at or below ${fmt(high)}`;
    if (toTop) return `at or above ${fmt(low)}`;
    return low === high ? `at ${fmt(low)}` : `between ${fmt(low)} and ${fmt(high)}`;
  });
  return parts.join(' or ');
}

/**
 * Round-number axis ticks (steps of 1, 2, 2.5 or 5 × 10^k): inside [from, to], or with ``cover``
 * reaching one step past each end so the axis can be widened to them.
 */
export function niceTicks(from: number, to: number, count = 6, cover = false): number[] {
  if (!(to > from)) return [];
  const raw = (to - from) / count;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((candidate) => candidate >= raw) ?? raw;
  const first = (cover ? Math.floor(from / step) : Math.ceil(from / step)) * step;
  const last = cover ? Math.ceil(to / step) * step : to;
  const ticks = [];
  for (let x = first; x <= last + step * 1e-9; x += step) ticks.push(Number(x.toFixed(10)));
  return ticks;
}

/** A smooth pre-expiry curve read at ``price`` by interpolation; null outside the priced range. */
export function curveAt(points: PayoffPoint[], price: number): number | null {
  const sorted = sortedPoints(points);
  if (sorted.length < 2 || price < sorted[0].price || price > sorted[sorted.length - 1].price) return null;
  return pnlAt(sorted, price);
}
