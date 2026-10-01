import { describe, expect, it } from 'vitest';
import { extremeRange, focusPayoffPoints, niceTicks, payoffTable, pnlAt, samplePayoff } from '../payoff';

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

describe('payoff readings', () => {
  // A 35/40 bull call spread bought for $1.50: loses $150 at or below 35, makes $350 at or above 40.
  const spread = [{ price: 0, pnl: -150 }, { price: 35, pnl: -150 }, { price: 40, pnl: 350 }, { price: 44, pnl: 350 }, { price: 80, pnl: 350 }];

  it('reads the P/L at any price, including between and beyond the points', () => {
    expect(pnlAt(spread, 36.5)).toBeCloseTo(0, 6);
    expect(pnlAt(spread, 30)).toBe(-150);
    expect(pnlAt(spread, 120)).toBe(350);
    const call = [{ price: 0, pnl: -210 }, { price: 35, pnl: -210 }, { price: 70, pnl: 3290 }];
    expect(pnlAt(call, 100)).toBeCloseTo(6290, 6);  // unbounded upside keeps its slope
  });

  it('samples densely and keeps the kinks exact', () => {
    const samples = samplePayoff(spread, 30, 45, [36.5], 31);
    expect(samples.length).toBeGreaterThanOrEqual(31);
    expect(samples.map((p) => p.price)).toEqual(expect.arrayContaining([35, 36.5, 40]));
    expect(samples.find((p) => p.price === 36.5)?.pnl).toBeCloseTo(0, 6);
  });

  it('lists the current price, moves, breakevens and strikes in price order', () => {
    const rows = payoffTable(spread, 37, [36.5], [35, 40]);  // -5% (35.15) sits on the 35 strike and gives way
    expect(rows.map((row) => row.price.toFixed(2))).toEqual(['29.60', '33.30', '35.00', '36.50', '37.00', '38.85', '40.00', '40.70', '44.40']);
    const now = rows.find((row) => row.kind === 'now');
    expect(now).toMatchObject({ label: 'Now', movePct: 0 });
    expect(now?.pnl).toBeCloseTo(50, 6);
    expect(rows.find((row) => row.kind === 'breakeven')?.movePct).toBeCloseTo((36.5 / 37 - 1) * 100, 6);
    expect(payoffTable(spread, null, [36.5], [35]).map((row) => row.label)).toEqual(['Strike', 'Breakeven']);
  });

  it('merges a level that sits on another one', () => {
    const rows = payoffTable(spread, 35, [], [35, 40]);
    expect(rows.find((row) => row.price === 35)?.label).toBe('Now · Strike');
  });

  it('says where the maximum gain and loss hold', () => {
    expect(extremeRange(spread, -150, false)).toBe('at or below 35.00');
    expect(extremeRange(spread, 350, false)).toBe('at or above 40.00');
    const condor = [{ price: 0, pnl: -300 }, { price: 90, pnl: -300 }, { price: 95, pnl: 200 }, { price: 105, pnl: 200 }, { price: 110, pnl: -300 }, { price: 220, pnl: -300 }];
    expect(extremeRange(condor, 200, false)).toBe('between 95.00 and 105.00');
    expect(extremeRange(condor, -300, false)).toBe('at or below 90.00 or at or above 110.00');
    // A covered call's worst case is only at a price of zero.
    const covered = [{ price: 0, pnl: -9800 }, { price: 100, pnl: 200 }, { price: 300, pnl: 200 }];
    expect(extremeRange(covered, -9800, false)).toBe('if the stock goes to zero');
  });

  it('picks round axis ticks', () => {
    expect(niceTicks(27.4, 41.1)).toEqual([27.5, 30, 32.5, 35, 37.5, 40]);
    expect(niceTicks(1600, 1950)).toEqual([1600, 1700, 1800, 1900]);
    expect(niceTicks(-151, 349, 4, true)).toEqual([-200, 0, 200, 400]);
  });

  it('keeps the strikes and breakeven apart', () => {
    const narrow = [{ price: 0, pnl: -141.3 }, { price: 600, pnl: -141.3 }, { price: 601, pnl: -41.3 }, { price: 1202, pnl: -41.3 }];
    const rows = payoffTable(narrow, 598, [600.41], [600, 601]);
    expect(rows.map((row) => row.label).filter(Boolean)).toEqual(['Now', 'Strike', 'Breakeven', 'Strike']);
  });

  it('extends the window past the last point with the last slope', () => {
    const put = [{ price: 0, pnl: 24000 }, { price: 250, pnl: -1000 }, { price: 275, pnl: -1000 }, { price: 500, pnl: -1000 }];
    const focused = focusPayoffPoints(put, [250, 600]);
    expect(focused[focused.length - 1].price).toBeGreaterThan(600);
    expect(focused[focused.length - 1].pnl).toBe(-1000);
  });
});

