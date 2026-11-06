import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AssistantPanel } from '../AssistantPanel';

const api = vi.hoisted(() => ({
  getAssistant: vi.fn(), startAssistant: vi.fn(), stopAssistant: vi.fn(),
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
    minutesLeft: 370, thetaPerMin: -0.004, atLevels: [{ id: 'stop:SPY', label: 'SPY shares stop', price: 728, now: 0.05, in30m: 0.04 }] }] },
  positions: [{ key: 'SPY|C735', ticker: 'SPY', type: 'option', label: 'SPY 11/09 735C', pnlPct: 12.5, weightPct: 1.2, daysLeft: 0, source: 'live feed' }],
};
const running = { ...idle, running: true, horizon: '0dte', startedAt: '2026-11-09T09:50:00-05:00', tickers: [spy], seq: 1,
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
    expect(await screen.findByText(/Nothing runs until you press Start/)).toBeInTheDocument();
    expect(screen.getByLabelText('Assistant stocks')).toHaveValue('SPY');
    fireEvent.change(screen.getByLabelText('Assistant stocks'), { target: { value: 'spy, qqq' } });
    fireEvent.click(screen.getByRole('button', { name: /Start/ }));
    await waitFor(() => expect(api.startAssistant).toHaveBeenCalledWith(['SPY', 'QQQ'], '0dte'));
    expect(onShow).toHaveBeenCalledWith('SPY');
    expect(await screen.findByText('Pre-market high')).toBeInTheDocument();
    expect(screen.getByText('not tested')).toBeInTheDocument();
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
    expect(await screen.findByText(/Trading assistant: SPY/)).toBeInTheDocument();
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());
    act(() => {
      FakeEventSource.last!.emit('alert', { seq: 2, at: '2026-11-09T09:51:00-05:00', ticker: 'SPY', kind: 'level', title: 'SPY above Pre-market high 738.03', detail: '' });
      FakeEventSource.last!.emit('alert', { seq: 3, at: '2026-11-09T09:52:00-05:00', ticker: null, kind: 'session', title: 'Stopped', detail: '' });
      FakeEventSource.last!.emit('alert', { seq: 2, at: '2026-11-09T09:51:00-05:00', ticker: 'SPY', kind: 'level', title: 'repeat', detail: '' });
    });
    expect(notifications).toEqual(['SPY above Pre-market high 738.03']);
    fireEvent.click(screen.getByRole('button', { name: /Stop/ }));
    expect(await screen.findByText(/Last session: stopped from the page/)).toBeInTheDocument();
  });

  it('shows nothing when the desk is unavailable', async () => {
    api.getAssistant.mockRejectedValue(new Error('off'));
    const { container } = render(<AssistantPanel current="SPY" onShow={() => undefined} />);
    await waitFor(() => expect(api.getAssistant).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
