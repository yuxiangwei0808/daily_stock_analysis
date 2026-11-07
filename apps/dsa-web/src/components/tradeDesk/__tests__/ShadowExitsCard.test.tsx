import { act, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ShadowExitsCard } from '../ShadowExitsCard';
import { toCamelCase } from '../../../api/utils';

const api = vi.hoisted(() => ({ getShadowExits: vi.fn() }));
vi.mock('../../../api/tradeDesk', () => ({ tradeDeskApi: api }));

const row = { label: 'All positions', positions: 4, tickers: 3, finished: 2, running: 2, noExit: 0, ifSoldPct: -20, ifHeld5Pct: -35.5, ifHeld10Pct: -50, yoursPct: -40, soldMinusHeldPct: 30, soldMinusHeldMedian: 25, soldBetterPct: 100 };

describe('ShadowExitsCard', () => {
  afterEach(() => { vi.useRealTimers(); api.getShadowExits.mockReset(); });

  it('stays hidden without a broker account', async () => {
    api.getShadowExits.mockResolvedValue({ enabled: false });
    const { container } = render(<ShadowExitsCard />);
    await vi.waitFor(() => expect(api.getShadowExits).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('says nothing is sent, and that it is too early, with the results so far', async () => {
    api.getShadowExits.mockResolvedValue({
      enabled: true, note: 'Exit test: nothing is sent to moomoo.', recorded: 4, carried: 5, levelChanges: 11, since: '2026-11-04T15:00:00+00:00',
      verdict: 'too_early', finished: 2, needed: 30, months: 1, rows: [row], byTrigger: [{ ...row, label: 'Stop' }],
      recent: [
        { label: 'AAPL 11/20 300C', ticker: 'AAPL', kind: 'stop', kindLabel: 'Stop', at: '2026-11-04T15:00:00+00:00', problems: ['wide_quote'], ifSoldPct: null },
        { label: 'MU shares', ticker: 'MU', kind: 'trend', kindLabel: '20-day low', at: '2026-11-03T15:00:00+00:00', problems: [], ifSoldPct: -5, ifHeld10Pct: -9, finished: true },
        { label: 'USO 11/20 160/170C spread', ticker: 'USO', kind: 'target', kindLabel: 'Target', at: '2026-11-02T15:00:00+00:00', problems: [], skip: 'multi_leg', ifSoldPct: 12 },
        { label: 'NVDA shares', ticker: 'NVDA', kind: 'stop', kindLabel: 'Stop', at: '2026-11-01T15:00:00+00:00', ended: 'no_exit' },
        { label: 'SPY 11/05 680C', ticker: 'SPY', kind: 'rule_loss', kindLabel: 'Built-in option loss', at: '2026-10-30T15:00:00+00:00', ended: 'no_exit', gaveUp: true },
        { label: 'QQQ 11/05 600C', ticker: 'QQQ', kind: 'stop', kindLabel: 'Stop', at: '2026-10-29T15:00:00+00:00', problems: [], ifSoldPct: 8, waitS: 42 },
      ],
      speed: { fromPush: 3, fromSnapshot: 1, checkMs: { median: 42, p95: 90, n: 4 }, exitWaitS: { median: 0.5, p95: 42, n: 3 }, gaveUp: 1 },
      feed: { state: 'live', lastQuoteS: 0.4, positionsReadS: 6, stockQuoteS: { median: 0.61, p95: 1.2, n: 900 }, optionQuoteS: { median: 1.5, p95: 3, n: 40 }, bookS: { median: 0.07, p95: 0.3, n: 500 }, fillToPositionS: null },
    });
    render(<ShadowExitsCard />);
    const card = await screen.findByTestId('shadow-exits');
    expect(card).toHaveTextContent('nothing is sent to moomoo');
    expect(card).toHaveTextContent('Too early to judge: 2 of 30 positions finished, over 1 month (needs at least 2).');
    expect(card).toHaveTextContent('−35.5%');
    expect(card).toHaveTextContent('waiting: quote too wide');
    expect(card).toHaveTextContent('would sell at −5.0% on cost, held −9.0%');
    expect(card).toHaveTextContent('the first live version would skip it');
    expect(card).toHaveTextContent('no order: closed or expired first');
    expect(card).toHaveTextContent('11 plan level changes');
    expect(card).toHaveTextContent("5 levels were already crossed when the exit test started, so they aren't graded.");
    expect(screen.getByTestId('shadow-feed')).toHaveTextContent('Live: every quote is checked as it arrives. Last quote 0.4 s ago, positions read 6 s ago.');
    expect(card).toHaveTextContent('Delay today, median: stock quotes 0.61 s, option quotes 1.50 s, order books 0.07 s.');
    expect(card).toHaveTextContent('Crossings: 3 seen on pushed quotes, 1 on snapshots; checked 42 ms after arriving; orders waited 0.50 s (95% within 42 s); 1 gave up with no usable quote (medians).');
    expect(card).toHaveTextContent('no order: no usable quote within 5 minutes');
    expect(card).toHaveTextContent('would sell at +8.0% on cost after 42 s');
  });

  it('shows a verdict once there is enough to judge, and an empty state before anything fires', async () => {
    api.getShadowExits.mockResolvedValue({ enabled: true, note: 'n', recorded: 40, levelChanges: 1, verdict: 'holding_better', finished: 40, months: 3, t: -4.5, tNeeded: 4.3, rows: [], byTrigger: [], recent: [] });
    const first = render(<ShadowExitsCard />);
    expect(await screen.findByTestId('shadow-exits')).toHaveTextContent('Holding beat selling: 40 positions over 3 months, t -4.5 (needs 4.3).');
    first.unmount();
    api.getShadowExits.mockResolvedValue({ enabled: true, note: 'n', recorded: 0, levelChanges: 0, rows: [], byTrigger: [], recent: [] });
    render(<ShadowExitsCard />);
    expect(await screen.findByTestId('shadow-exits')).toHaveTextContent('No would-be exits yet');
  });

  it('says plainly when the feed is not live, and shows the last measured day outside the session', async () => {
    const base = { enabled: true, note: 'n', recorded: 0, levelChanges: 0, rows: [], byTrigger: [], recent: [] };
    api.getShadowExits.mockResolvedValue({ ...base, feed: { state: 'down', errors: { feed: 'ProviderError' } } });
    const first = render(<ShadowExitsCard />);
    expect(await screen.findByTestId('shadow-feed')).toHaveTextContent('No live quotes right now, so nothing is being checked (ProviderError).');
    first.unmount();
    api.getShadowExits.mockResolvedValue({ ...base, feed: { state: 'snapshot' } });
    const second = render(<ShadowExitsCard />);
    expect(await screen.findByTestId('shadow-feed')).toHaveTextContent('Pushed quotes have stopped');
    second.unmount();
    api.getShadowExits.mockResolvedValue({ ...base, feed: { state: 'closed' }, feedLastDay: { day: '2026-11-05', stockQuoteS: { median: 0.61, p95: 1.2, n: 900 } } });
    render(<ShadowExitsCard />);
    expect(await screen.findByTestId('shadow-feed')).toHaveTextContent('Outside the regular session.');
    expect(screen.getByTestId('shadow-exits')).toHaveTextContent('Delay on 2026-11-05, median: stock quotes 0.61 s.');
  });

  it('refreshes on its own, keeps the last results when a refresh fails, and stops when closed', async () => {
    vi.useFakeTimers();
    const base = { enabled: true, note: 'n', recorded: 0, levelChanges: 0, rows: [], byTrigger: [], recent: [] };
    api.getShadowExits.mockResolvedValue({ ...base, feed: { state: 'live', lastQuoteS: 1, positionsReadS: 2 } });
    const { unmount } = render(<ShadowExitsCard />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId('shadow-feed')).toHaveTextContent('Last quote 1 s ago');
    api.getShadowExits.mockResolvedValue({ ...base, feed: { state: 'live', lastQuoteS: 0.2, positionsReadS: 9 } });
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(screen.getByTestId('shadow-feed')).toHaveTextContent('Last quote 0.2 s ago');
    api.getShadowExits.mockRejectedValue(new Error('server down'));
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(screen.getByTestId('shadow-feed')).toHaveTextContent('Last quote 0.2 s ago');
    expect(screen.getByTestId('shadow-exits')).toHaveTextContent('Could not refresh: server down');
    unmount();
    const calls = api.getShadowExits.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(api.getShadowExits.mock.calls.length).toBe(calls);
  });

  it('shows why results could not load', async () => {
    api.getShadowExits.mockRejectedValue(new Error('server down'));
    render(<ShadowExitsCard />);
    expect(await screen.findByText('server down')).toBeInTheDocument();
  });

  it('reads the API field names', () => {
    expect(toCamelCase<{ ifHeld5Pct: number; ifHeld10Pct: number; soldMinusHeldPct: number; tNeeded: number }>({
      if_held_5_pct: 1, if_held_10_pct: 2, sold_minus_held_pct: 3, t_needed: 4,
    })).toEqual({ ifHeld5Pct: 1, ifHeld10Pct: 2, soldMinusHeldPct: 3, tNeeded: 4 });
    expect(toCamelCase<Record<string, unknown>>({
      stock_quote_s: { median: 1, p95: 2, n: 3 }, fill_to_position_s: null, last_quote_s: 1, positions_read_s: 2, gave_up: true, wait_s: 3,
      feed_last_day: { day: 'd' }, exit_wait_s: null, check_ms: null, quote_age_s: null,
    })).toEqual({
      stockQuoteS: { median: 1, p95: 2, n: 3 }, fillToPositionS: null, lastQuoteS: 1, positionsReadS: 2, gaveUp: true, waitS: 3,
      feedLastDay: { day: 'd' }, exitWaitS: null, checkMs: null, quoteAgeS: null,
    });
  });
});
