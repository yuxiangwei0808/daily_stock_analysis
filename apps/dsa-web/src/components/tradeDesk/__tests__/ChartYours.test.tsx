import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Chart } from 'klinecharts';
import { GradesCard, YoursCard } from '../ChartYours';

const api = vi.hoisted(() => ({ addChartMark: vi.fn(), addChartBox: vi.fn(), deleteChartYours: vi.fn(), getChartGrades: vi.fn() }));
vi.mock('../../../api/tradeDesk', () => ({ tradeDeskApi: api }));
vi.mock('klinecharts', () => ({ registerOverlay: vi.fn(), registerIndicator: vi.fn() }));

function fakeChart() {
  const created: Array<{ name: string; onDrawEnd?: (event: { overlay: { points: Array<{ value?: number }> } }) => void }> = [];
  const chart = { createOverlay: vi.fn((item) => { created.push(item); return 'draft'; }), removeOverlay: vi.fn() };
  return { ref: { current: chart as unknown as Chart }, created, chart };
}

describe('YoursCard', () => {
  afterEach(() => vi.clearAllMocks());

  it('records a mark with its stop and target, and lists yours with removal only before they trade', async () => {
    api.addChartMark.mockResolvedValue({});
    api.deleteChartYours.mockResolvedValue(undefined);
    const changed = vi.fn();
    const { ref } = fakeChart();
    render(<YoursCard ticker="SPY" lastPrice={733} chart={ref} onChanged={changed} yours={[
      { id: 'you|SPY|mark|a', kind: 'mark', status: 'waiting', direction: 'short', stop: 740, target: 720 },
      { id: 'you|SPY|box|b', kind: 'box', status: 'closed', direction: 'long', top: 731, bottom: 729, price: 731, stop: 729, target: 735, how: 'target', r: 1.95, twinR: 0.4, entryTime: '2026-11-06T10:00:00-05:00' },
    ]} />);
    expect(screen.getByText('Last 733.00: the stop goes below it, the target above.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Mark long here' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a stop and a target');
    fireEvent.change(screen.getByLabelText('Mark stop'), { target: { value: '728.5' } });
    fireEvent.change(screen.getByLabelText('Mark target'), { target: { value: '745' } });
    fireEvent.click(screen.getByRole('button', { name: 'Mark long here' }));
    await vi.waitFor(() => expect(changed).toHaveBeenCalled());
    expect(api.addChartMark).toHaveBeenCalledWith('SPY', { direction: 'long', stop: 728.5, target: 745 });
    expect(screen.getByText(/short from the next bar's open, stop 740.00, target 720.00/)).toBeInTheDocument();
    expect(screen.getByText('+1.95R')).toBeInTheDocument();
    expect(screen.getByText(/random timing \+0\.40R/)).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Remove' })).toHaveLength(1);  // the finished box stays on the record
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    await vi.waitFor(() => expect(api.deleteChartYours).toHaveBeenCalledWith('you|SPY|mark|a'));
  });

  it('keeps a box drawn on the chart', async () => {
    api.addChartBox.mockResolvedValue({});
    const { ref, created, chart } = fakeChart();
    render(<YoursCard ticker="SPY" lastPrice={733} chart={ref} onChanged={vi.fn()} yours={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /Draw a box/ }));
    expect(created[0].name).toBe('dsaDraftBox');
    expect(screen.getByRole('button', { name: /Click two corners/ })).toBeDisabled();
    act(() => created[0].onDrawEnd?.({ overlay: { points: [{ value: 731.256 }, { value: 728.9 }] } }));
    fireEvent.click(screen.getByRole('button', { name: 'Keep box 728.90–731.26' }));
    await vi.waitFor(() => expect(api.addChartBox).toHaveBeenCalledWith('SPY', { top: 731.256, bottom: 728.9 }));
    await vi.waitFor(() => expect(chart.removeOverlay).toHaveBeenCalledWith({ groupId: 'draft' }));
  });
});

describe('GradesCard', () => {
  it('says too early until enough trades are graded, and the verdict after', async () => {
    const row = { source: 'system', kind: 'fade', label: 'System: fade at a box', needed: 30, waiting: 2, open: 1, closed: 3, expired: 4, late: 1, void: 0, months: 1, tNeeded: null, twinR: 0.1, edge: -0.3 };
    api.getChartGrades.mockResolvedValue({ minTrades: 30, recent: [], kinds: [
      { ...row, graded: 3, meanR: -0.2, t: null, verdict: 'too_early' },
      { ...row, kind: 'mark', label: 'Your marks', graded: 31, meanR: 0.4, t: 2.5, tNeeded: 2.1, verdict: 'passes' },
    ] });
    render(<GradesCard version={0} />);
    expect(await screen.findByText('Too early: 3 of 30 graded')).toBeInTheDocument();
    expect(screen.getByText('Passes: beats random timing (t 2.50 ≥ 2.1) and made money')).toBeInTheDocument();
    expect(screen.getAllByText('2 · 1 · 4 · 1 late')).toHaveLength(2);
  });
});
