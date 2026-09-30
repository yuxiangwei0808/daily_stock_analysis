import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TradeJournalCard } from '../TradeJournalCard';

const api = vi.hoisted(() => ({ getTradeJournal: vi.fn(), refreshTradeJournal: vi.fn() }));
vi.mock('../../../api/tradeDesk', () => ({ tradeDeskApi: api }));

const stats = (trades: number, totalPnl: number) => ({ trades, totalPnl, winRate: 50, avgPnl: totalPnl / Math.max(trades, 1), avgReturnPct: 12.5, avgHoldDays: 3 });

describe('TradeJournalCard', () => {
  it('reads fills on request and shows the round trips grouped three ways', async () => {
    api.getTradeJournal.mockResolvedValue({ journal: null });
    api.refreshTradeJournal.mockResolvedValue({ journal: {
      builtAt: '2026-09-30T15:00:00Z', fills: 42, firstFill: '2025-10-02', total: stats(12, 1840),
      byType: [{ label: 'Long calls', ...stats(8, 2100) }, { label: 'Long stock', ...stats(4, -260) }],
      byHold: [{ key: 'same_day', label: 'Same day', ...stats(5, 900) }, { key: 'over_20', label: 'Over 20 days', ...stats(0, 0) }],
      bySignal: [{ key: 'agreed', label: 'Agreed with a signal', ...stats(3, 700) }],
      byUnderlying: [],
      best: [{ code: 'MU', ticker: 'MU', kind: 'call', position: 'long', opened: '2026-09-05T10:00:00', closed: '2026-09-25T10:00:00', pnl: 900, returnPct: 150, how: 'closed' }],
      worst: [],
      openLotsNote: 'Open positions are not counted until they are closed or expire.',
    } });
    render(<TradeJournalCard />);
    fireEvent.click(await screen.findByRole('button', { name: /Read fills from moomoo/ }));
    const card = await screen.findByTestId('trade-journal');
    expect(card).toHaveTextContent('12 round trips since 2025-10-02 · realized +$1,840 · win rate 50%');
    expect(card).toHaveTextContent('Long calls');
    expect(card).not.toHaveTextContent('Over 20 days');  // empty buckets are left out
    expect(card).toHaveTextContent('MU long calls · 09-05→09-25');
  });
});
