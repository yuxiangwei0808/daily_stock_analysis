import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { StrategyCandidate } from '../../../types/tradeDesk';
import { PayoffPanel } from '../PayoffPanel';

// A 35/40 bull call spread for $1.50 plus $1.30 fees, with the server's corner points.
const spread = {
  id: 'k2', strategy: 'bull_call_spread', title: 'Bull call spread 35/40', underlying: 'SOXS',
  legs: [
    { contractId: 'US.SOXS261009C35000', right: 'call', side: 'buy', quantity: 1, strike: 35, multiplier: 100, entryPrice: 2.1 },
    { contractId: 'US.SOXS261009C40000', right: 'call', side: 'sell', quantity: 1, strike: 40, multiplier: 100, entryPrice: 0.6 },
  ],
  payoff: {
    entryDebit: 150, fees: 1.3, maxGain: 348.7, maxLoss: 151.3, gainBound: 'bounded', lossBound: 'bounded', breakevens: [36.513],
    capitalRequired: 151.3, capitalNote: '', assignmentNote: '',
    points: [{ price: 0, pnl: -151.3 }, { price: 35, pnl: -151.3 }, { price: 40, pnl: 348.7 }, { price: 44, pnl: 348.7 }, { price: 80, pnl: 348.7 }],
  },
} as unknown as StrategyCandidate;

describe('PayoffPanel', () => {
  it('states the breakeven against the current price and where the extremes hold', () => {
    render(<PayoffPanel candidate={spread} spot={34.27} />);
    const panel = screen.getByTestId('payoff-panel');
    expect(panel).toHaveTextContent('Breakeven 36.51 · +6.5% from now');
    expect(panel).toHaveTextContent('Max gain +$349 at or above 40.00');
    expect(panel).toHaveTextContent('Max loss −$151 at or below 35.00');
    expect(panel).toHaveTextContent("If it expired at today's 34.27 −$151");
  });

  it('tabulates the P/L at moves from now and at each level', () => {
    render(<PayoffPanel candidate={spread} spot={34.27} />);
    const rows = within(screen.getByTestId('payoff-table')).getAllByRole('row').slice(1);
    const text = rows.map((row) => row.textContent);  // the move appears twice: its column and, on phones, under the price
    expect(text).toContain('34.27Now0.0%−$151−100.0%');
    expect(text).toContain('36.51Breakeven+6.5%+6.5%$0.000.0%');
    expect(text).toContain('37.70+10.0%+10.0%+$118+78.3%');
    expect(text).toContain('40.00Strike+16.7%+16.7%+$349+230.5%');
  });

  it('works without a current price', () => {
    render(<PayoffPanel candidate={spread} spot={null} />);
    const panel = screen.getByTestId('payoff-panel');
    expect(panel).toHaveTextContent('Breakeven 36.51');
    expect(panel).not.toHaveTextContent('from now');
    expect(within(screen.getByTestId('payoff-table')).getAllByRole('row')).toHaveLength(4);  // header, 35, 36.51, 40
  });

  it('adds the close-before-expiry curves and the daily time decay', () => {
    const early = {
      ...spread,
      payoff: {
        ...spread.payoff, thetaPerDay: -4.31,
        curves: [
          { label: 'now', at: '2026-09-29T16:00:00Z', points: [{ price: 30, pnl: -120 }, { price: 34.27, pnl: -21 }, { price: 40, pnl: 168 }] },
          { label: 'halfway', at: '2026-10-04T18:00:00Z', points: [{ price: 30, pnl: -140 }, { price: 40, pnl: 250 }] },
        ],
      },
    } as unknown as StrategyCandidate;
    render(<PayoffPanel candidate={early} spot={34.27} />);
    expect(screen.getByTestId('payoff-panel')).toHaveTextContent('Time decay −$4.31/day at today\'s price');
    expect(screen.getByTestId('payoff-legend')).toHaveTextContent('If closed Oct 4 (halfway)');
    const rows = within(screen.getByTestId('payoff-table')).getAllByRole('row');
    expect(rows[0]).toHaveTextContent('Close now');
    expect(rows.map((row) => row.textContent)).toContain('34.27Now0.0%−$151−100.0%−$21');
    expect(rows.map((row) => row.textContent)).toContain('27.42−20.0%−20.0%−$151−100.0%—');  // outside the priced range
  });
});

