import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PortfolioRisk } from '../PortfolioRisk';

const api = vi.hoisted(() => ({ getHoldingsRisk: vi.fn() }));
vi.mock('../../../api/tradeDesk', () => ({ tradeDeskApi: api }));

describe('PortfolioRisk', () => {
  it('shows index-move scenarios, totals and each holding', async () => {
    api.getHoldingsRisk.mockResolvedValue({
      asOf: '2026-09-30T15:00:00Z',
      rows: [
        { ticker: 'NVDA', price: 182, sharesEquiv: 30, deltaDollars: 5460, thetaPerDay: 0, thetaPartial: false, betaSpy: 2.09, betaQqq: 1.74, betaAssumed: false },
        { ticker: 'ZZZ', price: 50, sharesEquiv: -200, deltaDollars: -10000, thetaPerDay: -3.25, thetaPartial: true, betaSpy: null, betaQqq: null, betaAssumed: true },
      ],
      totals: { deltaDollars: -4540, spyBetaDollars: 1411, spyBetaPct: 2.8, thetaPerDay: -3.25, thetaPct: -0.01 },
      scenarios: [{ key: 'SPY-3', label: 'SPY -3%', pnl: -346.72, pct: -0.69 }, { key: 'QQQ-5', label: 'QQQ -5%', pnl: -481.56, pct: -0.96 }],
    });
    render(<PortfolioRisk syncedAt="2026-09-30T14:55:00Z" />);
    const card = await screen.findByTestId('portfolio-risk');
    expect(card).toHaveTextContent('If SPY -3% −$347 (−0.7%)');
    expect(card).toHaveTextContent('SPY-beta-weighted $1,411 (+3% of the account)');
    const rows = within(card).getAllByRole('row').slice(1).map((row) => row.textContent);
    expect(rows).toEqual(['NVDA30$5,4602.091.74—', 'ZZZ−200−$10,0001.00*1.00*−$3.25*']);
  });
});


it('shows unavailable exposure instead of zero totals when quotes are missing', async () => {
  api.getHoldingsRisk.mockResolvedValue({ asOf: '2026-09-30T15:00:00Z', complete: false, unavailableTickers: ['AAA'],
    rows: [{ ticker: 'AAA', sharesEquiv: null, deltaDollars: null, thetaPerDay: null, thetaPartial: true, betaAssumed: true }],
    totals: { deltaDollars: null, spyBetaDollars: null, thetaPerDay: null },
    scenarios: [{ key: 'SPY-3', label: 'SPY -3%', pnl: null, pct: null }] });
  render(<PortfolioRisk />);
  const card = await screen.findByTestId('portfolio-risk');
  expect(card).toHaveTextContent('Totals leave out AAA: no usable option price');
  expect(card).toHaveTextContent('If SPY -3% —');
  expect(card).not.toHaveTextContent('$0');
});
