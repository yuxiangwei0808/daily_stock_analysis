import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TrackRecordCard } from '../TrackRecordCard';

const api = vi.hoisted(() => ({ getTrackRecord: vi.fn() }));
vi.mock('../../../api/tradeDesk', () => ({ tradeDeskApi: api }));

describe('TrackRecordCard', () => {
  it('splits closed ideas by the NX slow tunnel and tags recent ones', async () => {
    api.getTrackRecord.mockResolvedValue({
      windowDays: 90,
      groups: { medium: { label: 'Approved · medium', open: 1, closed: 2, winRate: 50, avgReturnPct: 1.2, avgVsSpyPct: 0.4 } },
      byNx: {
        agree: { label: 'NX agrees', closed: 2, open: 1, winRate: 100, avgReturnPct: 3.1 },
        neutral: { label: 'NX neutral', closed: 0, open: 0 },
        against: { label: 'NX against', closed: 1, open: 0, winRate: 0, avgReturnPct: -2.5 },
      },
      recent: [{ ticker: 'SOXS', direction: 'long', verdict: 'medium', signalDay: '2026-09-29', status: 'open', returnPct: 0.5, nxAlignment: 'against' }],
    });
    render(<TrackRecordCard />);
    const nx = await screen.findByTestId('track-record-nx');
    expect(nx).toHaveTextContent('NX agrees');
    expect(nx).toHaveTextContent('+3.10%');
    expect(nx).toHaveTextContent('NX against');
    expect(nx).not.toHaveTextContent('NX neutral');
    expect(nx).toHaveTextContent('Too few finished ideas to judge NX yet');
    expect(screen.getByText(/NX against/, { selector: 'span' })).toBeInTheDocument();
  });

  it('shows report calls by NX state', async () => {
    api.getTrackRecord.mockResolvedValue({
      windowDays: 90, groups: {}, recent: [],
      verdicts: {
        bullish: { label: 'Bullish calls', byNx: { all: { closed: 0, open: 0 } } },
        watch: { label: 'Watch', byNx: { above: { closed: 4, open: 0, avg10dVsPeersPct: 1.5 }, below: { closed: 3, open: 0, avg10dVsPeersPct: -2 }, all: { closed: 7, open: 5 } } },
      },
    });
    render(<TrackRecordCard />);
    const table = await screen.findByTestId('track-record-verdicts');
    expect(table).toHaveTextContent('Watch');
    expect(table).toHaveTextContent('+1.50%');
    expect(table).toHaveTextContent('-2.00%');
    expect(table).not.toHaveTextContent('Bullish calls');
    expect(table).toHaveTextContent('few have closed so far');
  });

  it('hides the NX split when no record has an NX state yet', async () => {
    api.getTrackRecord.mockResolvedValue({ windowDays: 90, groups: {}, byNx: { agree: { label: 'NX agrees', closed: 0, open: 0 } }, recent: [] });
    render(<TrackRecordCard />);
    await screen.findByText(/.+/, { selector: 'h2' });
    expect(screen.queryByTestId('track-record-nx')).not.toBeInTheDocument();
  });

  it('lists every source side by side with a cautious verdict', async () => {
    api.getTrackRecord.mockResolvedValue({
      windowDays: 90, groups: {}, recent: [],
      scoreboard: [
        { key: 'idea:high', label: 'Trade ideas · high conviction', horizon: 'to stop or target, ≤15 sessions', closed: 36, open: 4, avgVsPeersPct: 1.4, t: 2.6, verdict: 'ahead' },
        { key: 'youtube:Meet Kevin', label: 'YouTube · Meet Kevin', horizon: '20 sessions', closed: 5, open: 9, avgVsPeersPct: 3.0, t: null, verdict: 'too_early' },
        { key: 'social', label: 'Most discussed on social media', horizon: '20 sessions', closed: 30, open: 9, avgVsPeersPct: -17.2, t: null, verdict: 'too_early' },
      ],
    });
    render(<TrackRecordCard />);
    const rows = (await screen.findByTestId('scoreboard')).querySelectorAll('tbody tr');
    expect(rows[0]).toHaveTextContent('Trade ideas · high conviction');
    expect(rows[0]).toHaveTextContent('Ahead of same-day stocks');
    expect(rows[1]).toHaveTextContent('Too early (5/30)');
    expect(rows[1]).toHaveTextContent('+3.00%');
    expect(rows[2]).toHaveTextContent('Too early (needs a second month)');
  });

  it('shows the review value and the model calls once there is something to judge', async () => {
    api.getTrackRecord.mockResolvedValue({
      windowDays: 90, groups: {}, recent: [],
      reviewValue: { days: 17, gapPct: 1.63, t: 2.04, verdict: 'no_difference' },
      modelCalls: {
        buy: { label: 'Buy', closed: 135, open: 38, shown: 0, avg10dVsWatchlistPct: 0.15 },
        sell: { label: 'Sell', closed: 76, open: 78, shown: 0, avg10dVsWatchlistPct: -2.29 },
        hold: { label: 'Hold', closed: 0, open: 1, shown: 0 },
      },
    });
    render(<TrackRecordCard />);
    expect(await screen.findByTestId('track-record-review')).toHaveTextContent('approved minus rejected is +1.63% per idea: not a clear difference yet');
    const calls = screen.getByTestId('track-record-model-calls');
    expect(calls).toHaveTextContent('Buy 135 · +0.15% | Sell 76 · -2.29%');
    expect(calls).not.toHaveTextContent('Hold');
  });

  it('does not judge the review too early', async () => {
    api.getTrackRecord.mockResolvedValue({ windowDays: 90, groups: {}, recent: [], reviewValue: { days: 3, gapPct: 9, verdict: 'too_early' } });
    render(<TrackRecordCard />);
    await screen.findByText(/.+/, { selector: 'h2' });
    expect(screen.queryByTestId('track-record-review')).not.toBeInTheDocument();
  });
});

