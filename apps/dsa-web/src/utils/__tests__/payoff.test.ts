import { describe, expect, it } from 'vitest';
import { focusPayoffPoints } from '../payoff';

describe('focusPayoffPoints', () => {
  it('windows a narrow spread around its strikes and keeps exact values at the edges', () => {
    const points = [{ price: 0, pnl: -1151 }, { price: 1740, pnl: -1151 }, { price: 1762.5, pnl: 1099 }, { price: 3525, pnl: 1099 }];
    const focused = focusPayoffPoints(points, [1740, 1762.5, 1751.51]);
    expect(focused[0].price).toBeCloseTo(1762.5 - 1762.5 * 0.08 - 22.5, 0);
    expect(focused[0].pnl).toBe(-1151);
    expect(focused.map((p) => p.price)).toContain(1740);
    expect(focused[focused.length - 1].price).toBeLessThan(1920);
    expect(focused[focused.length - 1].pnl).toBe(1099);
  });

  it('interpolates inside a sloped segment and leaves data without anchors unchanged', () => {
    const points = [{ price: 0, pnl: -500 }, { price: 100, pnl: -500 }, { price: 200, pnl: 9500 }];
    const focused = focusPayoffPoints(points, [100, 105]);
    const last = focused[focused.length - 1];
    expect(last.pnl).toBeCloseTo(-500 + (last.price - 100) * 100, 6);
    expect(focusPayoffPoints(points, [])).toEqual(points);
  });
});
