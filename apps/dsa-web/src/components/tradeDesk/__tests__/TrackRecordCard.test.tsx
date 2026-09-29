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
    expect(nx).toHaveTextContent('Too few closed ideas to judge NX yet');
    expect(screen.getByText(/NX against/, { selector: 'span' })).toBeInTheDocument();
  });

  it('hides the NX split when no record has an NX state yet', async () => {
    api.getTrackRecord.mockResolvedValue({ windowDays: 90, groups: {}, byNx: { agree: { label: 'NX agrees', closed: 0, open: 0 } }, recent: [] });
    render(<TrackRecordCard />);
    await screen.findByText(/.+/, { selector: 'h2' });
    expect(screen.queryByTestId('track-record-nx')).not.toBeInTheDocument();
  });
});
