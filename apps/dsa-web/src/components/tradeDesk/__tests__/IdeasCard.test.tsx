import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { IdeasCard } from '../IdeasCard';

const api = vi.hoisted(() => ({ getRecentIdeas: vi.fn() }));
vi.mock('../../../api/tradeDesk', () => ({ tradeDeskApi: api, tradeDeskTicker: (code: string) => code }));

const rule = (id: string, label: string) => ({ id, verdict: 'no edge shown', label });
const ideas = {
  days: 7,
  rules: { idea: [rule('swing_trend', 'Swing trend, breakout, pullback (daily): no edge shown (within ±0.3%)'),
    rule('model_review', 'Model review of trade ideas: no edge shown: no measurable value (|t| ≤ 1)')],
  breakout: [rule('swing_trend', 'Swing trend, breakout, pullback (daily): no edge shown (within ±0.3%)')] },
  discord: { idea: false, breakout: false },
  items: [
    { id: 'idea:1:MU', kind: 'idea', verdict: 'high', ticker: 'MU', direction: 'long', signalDay: '2026-11-05', entry: 120, stop: 114, target: 132, status: 'open', returnPct: 1.25 },
    { id: 'breakout:1:AMD', kind: 'breakout', verdict: 'breakout', ticker: 'AMD', direction: 'long', signalDay: '2026-11-04', entry: 200, stop: 192, target: 216, status: 'closed', returnPct: -4, reason: 'stop' },
    { id: 'idea:1:XYZ', kind: 'idea', verdict: 'rejected', ticker: 'XYZ', direction: 'short', signalDay: '2026-11-05', entry: 50, stop: 53, target: 44, status: 'open' },
  ],
};

describe('IdeasCard', () => {
  afterEach(() => vi.clearAllMocks());

  it('lists the ideas with their rules verdicts and says they are not sent to Discord', async () => {
    api.getRecentIdeas.mockResolvedValue(ideas);
    const onChart = vi.fn();
    render(<IdeasCard onChart={onChart} />);
    expect(await screen.findByText('Trade ideas, last 7 days')).toBeInTheDocument();
    expect(screen.getByText(/Model review of trade ideas: no edge shown/)).toBeInTheDocument();
    expect(screen.getAllByText(/Swing trend, breakout, pullback/)).toHaveLength(1);  // a rule shared by both kinds once
    expect(screen.getByText(/not sent to Discord/)).toBeInTheDocument();
    expect(screen.getByText('Review: high')).toBeInTheDocument();
    expect(screen.getByText('running +1.3%')).toBeInTheDocument();
    expect(screen.getByText('−4.0% · stop')).toBeInTheDocument();
    expect(screen.queryByText('XYZ')).not.toBeInTheDocument();  // rejected ones only on request
    fireEvent.click(screen.getByLabelText('Show the ones the review rejected'));
    expect(screen.getByText('Rejected by the review')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Chart MU'));
    expect(onChart).toHaveBeenCalledWith('MU');
  });

  it('shows nothing when the tracker is unavailable', async () => {
    api.getRecentIdeas.mockRejectedValue(new Error('no tracker'));
    const { container } = render(<IdeasCard onChart={() => undefined} />);
    await vi.waitFor(() => expect(api.getRecentIdeas).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
