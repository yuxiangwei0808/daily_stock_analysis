import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AssistantPanel } from '../AssistantPanel';
import { ShadowModeBar, ShadowModeProvider } from '../shadowMode';
import { toCamelCase } from '../../../api/utils';

const api = vi.hoisted(() => ({
  getAssistant: vi.fn(), startAssistant: vi.fn(), stopAssistant: vi.fn(), askAssistant: vi.fn(), getInPlay: vi.fn(),
  parseAssistantPlan: vi.fn(), addAssistantPlan: vi.fn(), removeAssistantPlan: vi.fn(),
  getAssistantStreamUrl: () => '/assistant/stream', readAssistant: (raw: unknown) => raw,
}));
vi.mock('../../../api/tradeDesk', () => ({ tradeDeskApi: api }));

class FakeEventSource {
  static last: FakeEventSource | null = null;
  listeners: Record<string, Array<(event: MessageEvent<string>) => void>> = {};
  constructor() { FakeEventSource.last = this; }
  addEventListener(name: string, fn: (event: MessageEvent<string>) => void) { (this.listeners[name] ||= []).push(fn); }
  emit(name: string, data: unknown) { (this.listeners[name] || []).forEach((fn) => fn({ data: JSON.stringify(data) } as MessageEvent<string>)); }
  close() { /* closed */ }
}

const idle = { running: false, horizon: null, horizons: { '0dte': '0DTE options', intraday: 'Intraday', swing: 'Swing' }, startedAt: null,
  stopped: null, maxTickers: 3, tickers: [], events: [], seq: 0, note: 'Nothing is sent to moomoo.' };
const spy = {
  ticker: 'SPY', price: 736, priceAt: '2026-11-09T09:50:02-05:00', changePct: 0.41, quoteAgeS: 0.3, loaded: true, error: null,
  levels: [{ id: 'pm_high', kind: 'pm_high', label: 'Pre-market high', price: 738.03, distancePct: 0.28, verdict: 'not tested', verdictLabel: 'x' },
    { id: 'stop:SPY', kind: 'stop', label: 'SPY shares stop', price: 728, distancePct: -1.09, verdict: null, verdictLabel: 'Your level' }],
  flags: [{ kind: 'fade', box: 'range', direction: 'short', state: 'armed', price: 741, stop: 743, target: 737, grade: 'No edge shown in backtest' }],
  contracts: { expiry: '2026-11-09', asOf: null, error: null, rows: [{ code: 'US.SPY-C736', right: 'call', strike: 736, expiry: '2026-11-09',
    bid: 1, ask: 1.1, mid: 1.05, spreadPct: 9.5, iv: 20, delta: 0.5, volume: 100, openInterest: 500, breakevenMovePct: 0.15,
    minutesLeft: 370, thetaPerMin: -0.004, atLevels: [{ id: 'stop:SPY', label: 'SPY shares stop', price: 728, now: 0.05, inHalfHour: 0.04 }] }] },
  positions: [{ key: 'SPY|C735', ticker: 'SPY', type: 'option', label: 'SPY 11/09 735C', pnlPct: 12.5, weightPct: 1.2, daysLeft: 0, source: 'live feed' }],
  plans: [],
  notes: [{ at: '2026-11-09T09:50:05-05:00', kind: 'note', text: 'SPY 736 sits under the pre-market high 738.03, an untested level.',
    model: 'Claude Code (claude-sonnet-5-5)', seconds: 5, call: 'short now, stop 738.03, target 735, out by 11:00' },
  { at: '2026-11-09T09:49:00-05:00', kind: 'held', text: 'A note was held back: it quoted 741.37, which the system does not show.' }],
};
const running = { ...idle, running: true, horizon: '0dte', startedAt: '2026-11-09T09:50:00-05:00', tickers: [spy], seq: 1,
  model: { callsToday: 4, cap: 150, failed: 0, medianSeconds: 6.2, by: [{ label: 'Claude Code (claude-sonnet-5-5)', calls: 4 }], lastError: null, noteMinutes: 3 },
  events: [{ seq: 1, at: '2026-11-09T09:50:00-05:00', ticker: null, kind: 'session', title: 'Watching SPY', detail: '0DTE options' }] };

describe('AssistantPanel', () => {
  const notifications: string[] = [];
  beforeEach(() => {
    vi.stubGlobal('EventSource', FakeEventSource);
    class FakeNotification {
      static permission = 'granted';
      static requestPermission = vi.fn(async () => 'granted');
      constructor(title: string) { notifications.push(title); }
    }
    vi.stubGlobal('Notification', FakeNotification);
  });
  afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); notifications.length = 0; });

  it('starts on the stocks you name and shows levels, contracts, positions and flags', async () => {
    api.getAssistant.mockResolvedValue(idle);
    api.startAssistant.mockResolvedValue(running);
    const onShow = vi.fn();
    render(<AssistantPanel current="SPY" onShow={onShow} />);
    expect(await screen.findByText(/Shadow mode is an add-on to normal mode/)).toBeInTheDocument();
    expect(screen.getByLabelText('Assistant stocks')).toHaveValue('SPY');
    fireEvent.change(screen.getByLabelText('Assistant stocks'), { target: { value: 'spy, qqq' } });
    fireEvent.click(screen.getByRole('button', { name: 'Turn on shadow mode' }));
    await waitFor(() => expect(api.startAssistant).toHaveBeenCalledWith(['SPY', 'QQQ'], '0dte'));
    expect(onShow).toHaveBeenCalledWith('SPY');
    expect(await screen.findByText('Pre-market high')).toBeInTheDocument();
    expect(within(screen.getByTestId('assistant-levels')).getByText('not tested')).toBeInTheDocument();
    expect(screen.getByText('SPY 11/09 735C')).toBeInTheDocument();
    expect(screen.getByText(/expires today/)).toBeInTheDocument();
    expect(screen.getByText('736.00 C')).toBeInTheDocument();
    expect(screen.getByText('+0.15%')).toBeInTheDocument();
    expect(screen.getByText('armed')).toBeInTheDocument();
  });

  it('alerts with a notification for new events, not for the session itself, and stops', async () => {
    api.getAssistant.mockResolvedValue(running);
    api.stopAssistant.mockResolvedValue({ ...idle, stopped: { at: '2026-11-09T10:00:00-05:00', reason: 'stopped from the page' } });
    render(<AssistantPanel current="" onShow={() => undefined} />);
    expect(await screen.findByText(/Shadow mode: SPY/)).toBeInTheDocument();
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());
    act(() => {
      FakeEventSource.last!.emit('alert', { seq: 2, at: '2026-11-09T09:51:00-05:00', ticker: 'SPY', kind: 'level', title: 'SPY above Pre-market high 738.03', detail: '' });
      FakeEventSource.last!.emit('alert', { seq: 3, at: '2026-11-09T09:52:00-05:00', ticker: null, kind: 'session', title: 'Stopped', detail: '' });
      FakeEventSource.last!.emit('alert', { seq: 2, at: '2026-11-09T09:51:00-05:00', ticker: 'SPY', kind: 'level', title: 'repeat', detail: '' });
    });
    expect(notifications).toEqual(['SPY above Pre-market high 738.03']);
    fireEvent.click(screen.getByRole('button', { name: 'Turn off shadow mode' }));
    expect(await screen.findByText(/Last session: stopped from the page/)).toBeInTheDocument();
  });

  it('shows the notes, takes a question and keeps notes silent', async () => {
    api.getAssistant.mockResolvedValue(running);
    api.askAssistant.mockResolvedValue({ at: '2026-11-09T09:52:00-05:00', kind: 'answer', text: 'ok' });
    render(<AssistantPanel current="SPY" onShow={() => undefined} />);
    expect(await screen.findByText(/sits under the pre-market high 738.03/)).toBeInTheDocument();
    expect(screen.getByText(/Call: short now, stop 738.03/)).toBeInTheDocument();
    expect(screen.getByText(/untested model call, graded against random timing/)).toBeInTheDocument();
    expect(within(screen.getByTestId('assistant-notes')).getByText(/A note was held back/)).toBeInTheDocument();
    expect(screen.getByText(/Model today: 4 of 150 calls/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Ask the assistant'), { target: { value: 'take profit here?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    await waitFor(() => expect(api.askAssistant).toHaveBeenCalledWith('SPY', 'take profit here?'));
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());
    act(() => FakeEventSource.last!.emit('alert', { seq: 5, at: '2026-11-09T09:53:00-05:00', ticker: 'SPY', kind: 'note', title: 'SPY: assistant note', detail: '' }));
    expect(notifications).toEqual([]);
  });

  it('offers today\'s in-play list and starts on its top three', async () => {
    api.getAssistant.mockResolvedValue(idle);
    api.getInPlay.mockResolvedValue({ day: '2026-11-09', madeAt: 'x', top: 5, failed: [], verdict: 'pass',
      label: 'In play at 09:45: pass as a range selector, not a trade rule',
      rows: ['TSLA', 'NVDA', 'AMD', 'SPY', 'QQQ'].map((ticker, k) => ({ ticker, s: 3 - k * 0.4, gapPct: 1, firstRange: 1, t: 1, rank: k + 1 })) });
    api.startAssistant.mockResolvedValue(running);
    render(<AssistantPanel current="" onShow={() => undefined} />);
    expect(await screen.findByText(/In play 2026-11-09 \(09:45\)/)).toBeInTheDocument();
    expect(screen.getByText(/not a trade rule/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /TSLA/ }));
    expect(screen.getByLabelText('Assistant stocks')).toHaveValue('TSLA');
    fireEvent.click(screen.getByRole('button', { name: /Shadow the top 3/ }));
    await waitFor(() => expect(api.startAssistant).toHaveBeenCalledWith(['TSLA', 'NVDA', 'AMD'], '0dte'));
  });

  it('shows nothing when the desk is unavailable', async () => {
    api.getAssistant.mockRejectedValue(new Error('off'));
    const { container } = render(<AssistantPanel current="SPY" onShow={() => undefined} />);
    await waitFor(() => expect(api.getAssistant).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('reads the state as the server sends it, through the camelCase conversion', async () => {
    // As the API sends it: a dict keyed "0dte", "in_half_hour", R and twin_R must survive camelCase conversion.
    const raw = {
      running: true, horizon: '0dte', horizons: { '0dte': '0DTE options', intraday: 'Intraday', swing: 'Swing' },
      started_at: '2026-11-09T14:50:00+00:00', stopped: null, max_tickers: 3, seq: 1, events: [], note: 'Nothing is sent to moomoo.',
      model: { calls_today: 4, cap: 150, failed: 0, median_seconds: 4.2, by: [{ label: 'Claude Code (claude-sonnet-5-5)', calls: 4 }],
        last_error: null, note_minutes: 3, order: 'Claude Code (claude-sonnet-5-5, xhigh), then Codex (gpt-6.1-sol, xhigh)' },
      tickers: [{ ticker: 'SPY', price: 736, price_at: null, change_pct: 0.41, quote_age_s: 0.3, loaded: true, error: null, flags: [],
        levels: [{ id: 'vwap', kind: 'vwap', label: 'VWAP', price: 735.1, distance_pct: -0.12, verdict: 'not tested', verdict_label: 'x' }],
        contracts: { expiry: '2026-11-09', as_of: null, error: null, rows: [{ code: 'US.SPY-C736', right: 'call', strike: 736,
          expiry: '2026-11-09', bid: 1, ask: 1.1, mid: 1.05, spread_pct: 9.5, iv: 20, delta: 0.5, volume: 100, open_interest: 500,
          breakeven: 737.1, breakeven_move_pct: 0.15, minutes_left: 370, theta_per_min: -0.004,
          at_levels: [{ id: 'vwap', label: 'VWAP', price: 735.1, now: 0.61, in_half_hour: 0.52 }] }] },
        positions: [], notes: [],
        plans: [{ id: 'you|SPY|plan|1', ticker: 'SPY', status: 'closed', source: 'you', kind: 'plan', direction: 'long',
          trigger: { kind: 'above', price: 736.5, bar_minutes: null }, stop: 735, target: 739, time_stop: '15:30', price: 736.5,
          how: 'time', exit: 737.6, R: 0.71, twin_R: -0.2, day: '2026-11-09' }] }],
    };
    api.getAssistant.mockResolvedValue(toCamelCase(raw));
    render(<AssistantPanel current="" onShow={() => undefined} />);
    expect(await screen.findByText(/Shadow mode: SPY · 0DTE options · since/)).toBeInTheDocument();
    expect(screen.getByText(/Claude Code \(claude-sonnet-5-5\) 4/)).toBeInTheDocument();
    expect(screen.getByText('0.61 → 0.52')).toBeInTheDocument();
    expect(screen.getByText(/time \+0\.71R, random timing −0\.20R/)).toBeInTheDocument();
    expect(screen.getByText(/In order: Claude Code \(claude-sonnet-5-5, xhigh\), then Codex \(gpt-6\.1-sol, xhigh\)\./)).toBeInTheDocument();
  });

  it('shows which tier wrote a note, the escalation and the watcher line', async () => {
    const watched = { ...spy, notes: [
      { at: '2026-11-09T09:57:00-05:00', kind: 'note', tier: 'analyst', text: 'Above 738.03 on volume.', model: 'Claude Code (claude-sonnet-5-5, xhigh)', seconds: 12 },
      { at: '2026-11-09T09:56:50-05:00', kind: 'note', tier: 'watcher', text: 'Closed above 738.03.', escalate: true, why: 'A 1-minute bar closed past it.', model: 'Claude Code (claude-haiku-5-5, low)', seconds: 2 },
      { at: '2026-11-09T09:53:00-05:00', kind: 'note', tier: 'watcher', text: 'Crossed 738.03.', escalate: false, why: 'No bar confirms it.' }] };
    api.getAssistant.mockResolvedValue({ ...running, tickers: [watched], model: { ...running.model,
      watch: { order: 'Claude Code (claude-haiku-5-5, low), then API', callsToday: 9, escalatedToday: 2, analystPerHour: 12,
        by: [], grade: { days: 3, decisions: 40, meanDifferencePct: 0.12, t: 1.1, verdict: 'too early: 3 of 30 graded days', forwardMinutes: 30 } } } });
    render(<AssistantPanel current="" onShow={() => undefined} />);
    expect(await screen.findByText('Closed above 738.03.')).toBeInTheDocument();
    expect(screen.getByText('→ Analyst: A 1-minute bar closed past it.')).toBeInTheDocument();
    expect(screen.getByText(/· No bar confirms it\./)).toBeInTheDocument();
    const notes = within(screen.getByTestId('assistant-notes'));
    expect(notes.getAllByText('Watcher')).toHaveLength(2);
    expect(notes.getByText('Analyst')).toBeInTheDocument();
    const line = screen.getByTestId('watcher-line');
    expect(line).toHaveTextContent('Watcher today: 9 reads, 2 sent to the analyst (at most 12 an hour');
    expect(line).toHaveTextContent('too early: 3 of 30 graded days (+0.12% vs the stock\'s other moments, t 1.1); untested.');
  });

  it('shows the watcher\'s latest quiet minute read apart from the notes, and its cadence', async () => {
    const quiet = { at: '2026-11-09T09:58:01-05:00', kind: 'note', tier: 'watcher', trigger: 'minute', text: 'SPY holds 736, a quiet drift.',
      escalate: false, why: 'Nothing new since the last read.' };
    const watched = { ...spy, watch: quiet, notes: [
      { at: '2026-11-09T09:57:00-05:00', kind: 'note', tier: 'analyst', text: 'Above 738.03 on volume.' }] };
    api.getAssistant.mockResolvedValue({ ...running, tickers: [watched], model: { ...running.model,
      watch: { order: 'Claude Code (claude-haiku-5-5, medium), then API', callsToday: 180, cap: 1500, escalatedToday: 3, analystPerHour: 12,
        everyMinute: true, eventSeconds: 10, by: [], grade: null } } });
    const { unmount } = render(<AssistantPanel current="" onShow={() => undefined} />);
    const latest = await screen.findByTestId('watcher-latest');
    expect(latest).toHaveTextContent('Watcher, each minute SPY holds 736, a quiet drift. · Nothing new since the last read.');
    expect(screen.getAllByRole('listitem').some((li) => li.textContent?.includes('quiet drift'))).toBe(false);
    expect(screen.getByTestId('watcher-line')).toHaveTextContent(
      'Watcher today: 180 reads (each stock every completed minute, events within 10 s; at most 1500 a day), 3 sent to the analyst');
    unmount();
    api.getAssistant.mockResolvedValue({ ...running, tickers: [{ ...watched, watch: { ...quiet, escalate: true } }] });
    render(<AssistantPanel current="" onShow={() => undefined} />);
    await screen.findByText('Above 738.03 on volume.');
    expect(screen.queryByTestId('watcher-latest')).toBeNull();  // an escalated read is in the notes instead
  });

  it('folds levels, box flags, contracts and model use to one line each, and keeps watcher notes out of the alerts', async () => {
    api.getAssistant.mockResolvedValue({ ...running, events: [
      { seq: 3, at: '2026-11-09T09:52:00-05:00', ticker: 'SPY', kind: 'note', title: 'SPY: watcher note', detail: 'SPY holds 736.' },
      { seq: 2, at: '2026-11-09T09:51:30-05:00', ticker: 'SPY', kind: 'note', title: 'SPY: assistant note', detail: 'Above 738.03.' },
      ...running.events] });
    render(<AssistantPanel current="" onShow={() => undefined} />);
    const levels = await screen.findByTestId('assistant-levels');
    for (const id of ['assistant-levels', 'assistant-flags', 'assistant-contracts', 'assistant-models', 'assistant-glossary']) {
      expect(screen.getByTestId(id)).not.toHaveAttribute('open');
    }
    expect(levels.querySelector('summary')).toHaveTextContent(/^Levels \(2\)· above: Pre-market high 738\.03 \(\+0\.28%\); below: SPY shares stop 728\.00 \(−1\.09%\)$/);
    const rows = Array.from(levels.querySelectorAll('tr')).map((tr) => tr.textContent);
    expect(rows[1]).toContain('Price now736.00');  // between the level above and the one below
    expect(screen.getByTestId('assistant-flags').querySelector('summary')).toHaveTextContent('1; nearest fade short at 741.00 (+0.68% away)');
    expect(screen.getByText(/fade short sells at the bottom edge/)).toBeInTheDocument();
    expect(screen.getByTestId('assistant-contracts').querySelector('summary')).toHaveTextContent('1 expiring 2026-11-09, strikes 736.00–736.00');
    expect(screen.getByRole('columnheader', { name: 'At SPY shares stop 728.00' })).toBeInTheDocument();
    expect(screen.getByTestId('assistant-models').querySelector('summary')).toHaveTextContent('analyst 4 of 150 calls');
    expect(screen.getByText('SPY: assistant note')).toBeInTheDocument();
    expect(screen.queryByText('SPY: watcher note')).toBeNull();
    // The labels explained: the last section of the card, after the alerts and model use.
    const glossary = screen.getByTestId('assistant-glossary');
    expect(glossary.querySelector('summary')).toHaveTextContent('What these mean');
    expect(glossary).toHaveTextContent('Fade short: Sell at a box\'s bottom edge when the price climbs back up to it from below');
    expect(glossary).toHaveTextContent('Nothing on this card is a tested trading signal.');
    const order = ['assistant-notes', 'assistant-levels', 'assistant-models', 'assistant-glossary'].map((id) => screen.getByTestId(id));
    for (let k = 1; k < order.length; k += 1) {
      expect(order[k - 1].compareDocumentPosition(order[k]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
  });

  it('names the mode on every tab: normal mode, or shadow mode on with a way to turn it off', async () => {
    api.getAssistant.mockResolvedValue(idle);
    const openChart = vi.fn();
    const { unmount } = render(<ShadowModeProvider><ShadowModeBar onOpenChart={openChart} /></ShadowModeProvider>);
    expect(await screen.findByText('Normal mode')).toBeInTheDocument();
    expect(screen.getByText(/Your alerts go to Discord/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Set up shadow mode' }));
    expect(openChart).toHaveBeenCalled();
    unmount();

    api.getAssistant.mockResolvedValue(running);
    api.stopAssistant.mockResolvedValue({ ...idle, stopped: { at: '2026-11-09T10:00:00-05:00', reason: 'stopped from the page' } });
    render(<ShadowModeProvider><ShadowModeBar onOpenChart={openChart} /></ShadowModeProvider>);
    expect(await screen.findByText('Shadow mode on')).toBeInTheDocument();
    expect(screen.getByText(/SPY · since .* ET · alerts sound on this page while it is open; Discord alerts carry on as usual/)).toBeInTheDocument();
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());
    act(() => FakeEventSource.last!.emit('alert', { seq: 4, at: '2026-11-09T09:55:00-05:00', ticker: 'SPY', kind: 'level', title: 'SPY below VWAP', detail: '' }));
    expect(notifications).toEqual(['SPY below VWAP']);  // the alert sounds with only the bar on the page (another tab)
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }));
    expect(await screen.findByText('Normal mode')).toBeInTheDocument();
    expect(api.stopAssistant).toHaveBeenCalled();
  });

  it('shares one stream between the mode line and the panel on the page', async () => {
    api.getAssistant.mockResolvedValue(running);
    const made = vi.fn();
    class CountingEventSource extends FakeEventSource { constructor() { super(); made(); } }
    vi.stubGlobal('EventSource', CountingEventSource);
    render(<ShadowModeProvider><ShadowModeBar onOpenChart={() => undefined} /><AssistantPanel current="" onShow={() => undefined} /></ShadowModeProvider>);
    expect(await screen.findByText(/Shadow mode: SPY/)).toBeInTheDocument();
    await waitFor(() => expect(made).toHaveBeenCalledTimes(1));
    expect(api.getAssistant).toHaveBeenCalledTimes(1);
  });
});
