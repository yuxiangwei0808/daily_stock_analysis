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
