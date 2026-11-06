import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { StopRiskCard } from '../StopRiskCard';

const api = vi.hoisted(() => ({ getStopRisk: vi.fn() }));
vi.mock('../../../api/tradeDesk', () => ({ tradeDeskApi: api }));

const risk = {
  asOf: '2026-11-06T15:00:00Z', gapMultiple: 2, totals: { atStopsPct: -1.25, atGapsPct: -2.6 }, withoutStop: ['SOXS shares'], unpriced: [],
  rows: [
    { key: 'NVDA', ticker: 'NVDA', label: 'NVDA shares', type: 'stock', exposure: 'long', weightPct: 10, stop: 190, stopDistancePct: -5, atStopPct: -0.5, atGapPct: -1, pastStop: false, unavailable: null },
    { key: 'USO', ticker: 'USO', label: 'USO 10/16 160/170C spread', type: 'option', exposure: 'long', weightPct: 5.4, stop: 145, stopDistancePct: -3.3, atStopPct: -0.75, atGapPct: -1.6, pastStop: false, unavailable: null },
    { key: 'SOXS', ticker: 'SOXS', label: 'SOXS shares', type: 'stock', exposure: 'long', weightPct: 3, stop: null, atStopPct: null, atGapPct: null, pastStop: false, unavailable: 'no stop' },
  ],
};

describe('StopRiskCard', () => {
  afterEach(() => vi.clearAllMocks());

  it('shows the loss at each stop and the totals as percentages of the account only', async () => {
    api.getStopRisk.mockResolvedValue(risk);
    const { container } = render(<StopRiskCard />);
    expect(await screen.findByText('Risk at your stops')).toBeInTheDocument();
    expect(screen.getByText('−1.25%')).toBeInTheDocument();
    expect(screen.getByText('−2.60%')).toBeInTheDocument();
    expect(screen.getByText('190 (−5.0%)')).toBeInTheDocument();
    expect(screen.getByText('no stop')).toBeInTheDocument();
    expect(screen.getByText(/No stop: SOXS shares/)).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/\$/);  // no dollar amounts
  });

  it('on the chart shows only the charted stock, and nothing without a position or a broker account', async () => {
    api.getStopRisk.mockResolvedValue(risk);
    render(<StopRiskCard ticker="USO" />);
    expect(await screen.findByText('Risk at your USO stops')).toBeInTheDocument();
    expect(screen.queryByText('NVDA shares')).not.toBeInTheDocument();
    const other = render(<StopRiskCard ticker="SPY" />);
    await vi.waitFor(() => expect(api.getStopRisk).toHaveBeenCalledTimes(2));
    expect(other.container).toBeEmptyDOMElement();
    api.getStopRisk.mockRejectedValue(new Error('Set TRADE_DESK_BROKER_ACCOUNT'));
    const none = render(<StopRiskCard />);
    await vi.waitFor(() => expect(api.getStopRisk).toHaveBeenCalledTimes(3));
    expect(none.container).toBeEmptyDOMElement();
  });
});
