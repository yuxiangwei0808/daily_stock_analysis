import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ChartSnapshot } from '../../../types/tradeDesk';

const chart = vi.hoisted(() => {
  const state = { loader: null as null | { getBars: (params: { type: string; callback: (data: unknown[], more?: boolean) => void }) => void }, overlays: [] as Array<{ name: string; groupId?: string; points: unknown[]; extendData: Record<string, unknown> }>, bars: [] as unknown[] };
  const instance = {
    setDataLoader: vi.fn((loader) => { state.loader = loader; }),
    createIndicator: vi.fn(), overrideIndicator: vi.fn(), setStyles: vi.fn(), resize: vi.fn(),
    setSymbol: vi.fn(() => state.loader?.getBars({ type: 'init', callback: (data) => { state.bars = data; } })),
    setPeriod: vi.fn(), resetData: vi.fn(() => state.loader?.getBars({ type: 'init', callback: (data) => { state.bars = data; } })),
    removeOverlay: vi.fn((filter?: { groupId?: string }) => { state.overlays = state.overlays.filter((item) => filter?.groupId && item.groupId !== filter.groupId); }),
    createOverlay: vi.fn((items) => { state.overlays.push(...items); }),
  };
  return { state, instance };
});
vi.mock('klinecharts', () => ({ init: vi.fn(() => chart.instance), dispose: vi.fn(), registerOverlay: vi.fn(), registerIndicator: vi.fn() }));

const api = vi.hoisted(() => ({ getChart: vi.fn(), getHoldings: vi.fn(), getTrackRecord: vi.fn() }));
vi.mock('../../../api/tradeDesk', async (original) => ({ ...(await original<typeof import('../../../api/tradeDesk')>()), tradeDeskApi: api }));
vi.mock('../../../api/systemConfig', () => ({ systemConfigApi: { getWatchlist: vi.fn().mockResolvedValue(['SPY', '600519', 'hk00700']) } }));

import { ChartPanel } from '../ChartPanel';
import { overlaysFor, shortLabel } from '../chartOverlays';

const grade = { verdict: 'no edge shown', t: [-1.78, -0.9], label: 'No edge shown in backtest (t -1.78 / -0.90 vs random timing)', source: 'docs' };
const snapshot = (interval: ChartSnapshot['interval'] = '15m'): ChartSnapshot => ({
  ticker: 'SPY', interval, asOf: '2026-11-05T21:00:00+00:00', params: { impulseT: 1, tidy: false },
  bars: [
    { time: '2026-11-05T15:45:00-05:00', start: '2026-11-05T15:30:00-05:00', open: 734, high: 735, low: 733, close: 734.5, volume: 10, partial: false },
    { time: '2026-11-05T16:00:00-05:00', start: '2026-11-05T15:45:00-05:00', open: 734.5, high: 736.4, low: 732.71, close: 733, volume: 12, partial: false },
  ],
  nx: { a: [null, 744.09], b: [null, 740.66], a1: [null, 753.96], b1: [null, 751.32] },
  note: 'Boxes and flags follow the backtested rules.', warnings: [],
  boxes: [{ setup: '2026-11-05T10:00:00-05:00', box: 'range', bottom: 764.15, top: 768.25, wickBottom: 763.9, wickTop: 768.6, up: true,
    from: '2026-11-05T10:00:00-05:00', to: '2026-11-05T12:00:00-05:00', until: '2026-11-12T16:00:00-05:00', alive: true }],
  flags: [
    { kind: 'fade', setup: '2026-11-05T10:00:00-05:00', box: 'base', direction: 'short', price: 766.81, stop: 768.82, target: 762.79, rewardRisk: 2, grade, state: 'armed', time: null, result: null },
    { kind: 'fade', setup: '2026-11-03T10:00:00-05:00', box: 'range', direction: 'long', price: 756.41, stop: 753.51, target: 762.21, rewardRisk: 2, grade, state: 'closed',
      time: '2026-11-05T15:00:00-05:00', result: { how: 'stop', r: -1.13, exit: 753.51, exitTime: '2026-11-05T15:30:00-05:00' } },
  ],
  levels: [{ kind: 'vah', price: 773.3434, label: 'Value-area high, last week' }, { kind: 'prior_close', price: 733, label: 'Prior day close (Nov 5)' },
    { kind: 'stop', price: 711.01, label: 'SPY shares stop (daily review)', positionKey: 'k' }],
  typicalRange: { t: 6.015, impulse: 6.015, barsThrough: '2026-11-05T16:00:00-05:00' },
});
const colors = { up: 'hsl(1 1% 1%)', down: 'hsl(2 2% 2%)', text: 'hsl(3 3% 3%)', muted: 'hsl(4 4% 4%)', grid: 'hsl(5 5% 5%)', primary: 'hsl(6 6% 6%)', warning: 'hsl(7 7% 7%)', purple: 'hsl(8 8% 8%)' };

describe('ChartPanel', () => {
  afterEach(() => { vi.clearAllMocks(); chart.state.overlays = []; });

  it('charts a held stock with its boxes, lines and the armed flag and its backtest grade', async () => {
    api.getHoldings.mockResolvedValue({ view: { stocks: [{ ticker: 'SPY' }], options: [{ underlying: 'QQQ', expired: false }, { underlying: 'OLD', expired: true }] } });
    api.getTrackRecord.mockResolvedValue({ recent: [{ ticker: 'MSFT', kind: 'idea' }, { ticker: 'IWM', kind: 'breakout' }] });
    api.getChart.mockResolvedValue(snapshot());
    render(<ChartPanel />);
    expect(screen.getByText('Pick a stock')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'MSFT' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'IWM' })).not.toBeInTheDocument();  // a breakout alert is not a trade idea
    expect(screen.queryByRole('button', { name: 'OLD' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '600519' })).not.toBeInTheDocument();  // not a US ticker
    fireEvent.click(screen.getAllByRole('button', { name: 'SPY' })[0]);
    expect(await screen.findByText('Fade short')).toBeInTheDocument();
    expect(api.getChart).toHaveBeenCalledWith('SPY', '15m', 1, false);
    expect(screen.getByText(/766\.81 · stop 768\.82 · target 762\.79 · 2:1 · \+4\.61% from last/)).toBeInTheDocument();
    expect(screen.getByText(/No edge shown in backtest \(t -1\.78/)).toBeInTheDocument();
    expect(chart.state.bars).toHaveLength(2);
    const names = chart.state.overlays.map((item) => `${item.name}:${item.extendData.label}`);
    expect(names).toContain('dsaBox:Range 764.15–768.25');
    expect(names).toContain('dsaLevel:VAH −1w 773.34');
    expect(names).toContain('dsaLevel:Your stop 711.01');
    expect(names).toContain('dsaMark:fade −1.1R');
    expect(names).toContain('dsaLevel:Armed: Fade short 766.81');  // the selected flag's entry, stop and target
    expect(screen.getByText('Value-area high, last week')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /finished trade/ }));
    expect(screen.getByText(/stop at 753\.51/)).toBeInTheDocument();
  });

  it('keeps the lines but not the boxes on daily bars, and asks for the tested variant', async () => {
    api.getHoldings.mockResolvedValue({ view: null });
    api.getTrackRecord.mockRejectedValue(new Error('off'));
    api.getChart.mockImplementation((_t: string, interval: ChartSnapshot['interval']) => Promise.resolve(snapshot(interval)));
    render(<ChartPanel initialTicker="SPY" />);
    await screen.findByText('Fade short');
    fireEvent.change(screen.getByLabelText('Box rules'), { target: { value: '0.6-tidy' } });
    fireEvent.click(screen.getByRole('button', { name: '1d' }));
    expect(await screen.findByText(/Boxes come from 15-minute bars/)).toBeInTheDocument();
    expect(api.getChart).toHaveBeenLastCalledWith('SPY', '1d', 0.6, true);
    expect(chart.state.overlays.some((item) => item.name === 'dsaBox')).toBe(false);
    expect(chart.state.overlays.some((item) => item.name === 'dsaLevel')).toBe(true);
  });

  it('shows why moomoo could not be read', async () => {
    api.getHoldings.mockResolvedValue({ view: null });
    api.getTrackRecord.mockResolvedValue({ recent: [] });
    api.getChart.mockRejectedValue(new Error('connection_unavailable: OpenD is not reachable'));
    render(<ChartPanel initialTicker="SPY" />);
    expect(await screen.findByText(/OpenD is not reachable/)).toBeInTheDocument();
  });
});

describe('chart overlays', () => {
  it('places a 15-minute box on finer bars by the last bar inside it, and stops it at the last bar', () => {
    const items = overlaysFor(snapshot('5m'), colors);
    const box = items.find((item) => item.name === 'dsaBox');
    const points = box?.points as Array<{ timestamp: number; value: number }>;
    expect(points[0]).toEqual({ timestamp: Date.parse('2026-11-05T10:00:00-05:00'), value: 768.25 });
    expect(points[1].timestamp).toBe(Date.parse('2026-11-05T11:55:00-05:00'));
    expect(points[2].timestamp).toBe(Date.parse('2026-11-05T15:45:00-05:00'));  // alive until next week: drawn to the last bar
  });

  it('names lines briefly on the chart', () => {
    expect(shortLabel({ kind: 'val', price: 762.5142, label: 'Value-area low, 2 weeks ago' })).toBe('VAL −2w 762.51');
    expect(shortLabel({ kind: 'prior_high', price: 0.51234, label: 'Prior day high (Nov 5)' })).toBe('PDH 0.5123');
    expect(shortLabel({ kind: 'alert', price: 158, label: 'Your alert: price at or above 158' })).toBe('Your alert 158.00');
  });
});
