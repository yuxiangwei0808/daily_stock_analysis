import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PositionAsk } from '../PositionAsk';
import type { HoldingRule, PositionQuestion } from '../../../types/tradeDesk';

const api = vi.hoisted(() => ({
  getPositionQuestions: vi.fn(),
  askAboutPositions: vi.fn(),
  deletePositionQuestion: vi.fn(),
  createHoldingRule: vi.fn(),
}));
vi.mock('../../../api/tradeDesk', () => ({ tradeDeskApi: api }));

const choices = [
  { key: 'SOFI', ticker: 'SOFI', label: 'SOFI shares' },
  { key: 'SQQQ 2026-10-16', ticker: 'SQQQ', label: 'SQQQ 10/16 35C' },
];

const answered: PositionQuestion = {
  id: 'q1', question: 'Where should my stop-loss be?', positionKey: null, status: 'done',
  createdAt: '2026-10-07T20:00:00Z', model: 'claude', summary: 'Keep SOFI with a stop under the 20-day low.',
  positions: [
    { key: 'SOFI', ticker: 'SOFI', type: 'stock', exposure: 'long', label: 'SOFI shares', price: 18.4, pnlPct: -6.4,
      action: 'hold', stop: 17.8, stopBasis: 'below the 20-day low 18.10', stopSource: 'model', target: 21.0,
      targetBasis: 'the September high', targetSource: 'model', reason: 'The trend is intact above the 50-day.',
      risk: 'Earnings Oct 28.' },
    { key: 'SQQQ 2026-10-16', ticker: 'SQQQ', type: 'option', exposure: 'long', label: 'SQQQ 10/16 35C · -52.0% on cost',
      price: 33.1, pnlPct: -52, action: 'close', stop: 31.9, stopBasis: '1.5 ATR rule of thumb (the model’s level was on the wrong side)',
      stopSource: 'rule', target: null, pnlStopPct: -60, pnlTargetPct: null, reason: 'Little time left out of the money.' },
  ],
};

const rule = (kind: HoldingRule['kind'], value: number, positionKey = 'SOFI'): HoldingRule => ({
  id: `${kind}-${value}`, ticker: 'SOFI', positionKey, kind, value, repeat: 'once', status: 'active', createdAt: '2026-10-07T20:00:00Z',
});

describe('PositionAsk', () => {
  beforeEach(() => {
    Object.values(api).forEach((method) => method.mockReset());
    api.getPositionQuestions.mockResolvedValue([answered]);
    api.createHoldingRule.mockResolvedValue({});
  });

  it('shows the action and levels per position and turns a level into an alert', async () => {
    const changed = vi.fn();
    render(<PositionAsk choices={choices} rules={[rule('price_above', 21)]} onRulesChanged={changed} />);
    const sofi = await screen.findByTestId('position-answer-SOFI');
    expect(sofi).toHaveTextContent('Hold');
    expect(sofi).toHaveTextContent('17.80');
    expect(sofi).toHaveTextContent('below the 20-day low 18.10');
    expect(within(sofi).getByText('Alert set')).toBeInTheDocument();  // the target already has one
    fireEvent.click(within(sofi).getByRole('button', { name: 'Set stop alert for SOFI' }));
    await waitFor(() => expect(api.createHoldingRule).toHaveBeenCalledWith(expect.objectContaining({
      positionKey: 'SOFI', ticker: 'SOFI', kind: 'price_below', value: 17.8, repeat: 'once' })));
    expect(changed).toHaveBeenCalled();
    const option = screen.getByTestId('position-answer-SQQQ 2026-10-16');
    expect(option).toHaveTextContent('Close');
    expect(option).toHaveTextContent('SQQQ at 31.90');
    expect(option).toHaveTextContent('-60% on cost');
    fireEvent.click(within(option).getByRole('button', { name: 'Set cut at p&l alert for SQQQ' }));
    await waitFor(() => expect(api.createHoldingRule).toHaveBeenLastCalledWith(expect.objectContaining({
      positionKey: 'SQQQ 2026-10-16', kind: 'pnl_below', value: -60 })));
  });

  it('asks about the chosen position, polls while it is answered and reviews all from a quick question', async () => {
    api.getPositionQuestions.mockResolvedValueOnce([]);
    api.askAboutPositions.mockResolvedValue({ id: 'q2', question: 'Where should my stop-loss be?', positionKey: 'SOFI',
      status: 'running', createdAt: '2026-10-07T20:05:00Z' });
    render(<PositionAsk choices={choices} rules={[]} onRulesChanged={vi.fn()} />);
    fireEvent.change(await screen.findByLabelText('Position'), { target: { value: 'SOFI' } });
    fireEvent.click(screen.getByRole('button', { name: 'Where should my stop-loss be?' }));
    await waitFor(() => expect(api.askAboutPositions).toHaveBeenCalledWith('Where should my stop-loss be?', 'SOFI'));
    expect(await screen.findByText(/Reading your positions/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled();  // one question at a time
    await waitFor(() => expect(screen.getByTestId('position-answer-SOFI')).toBeInTheDocument(), { timeout: 5000 });
    fireEvent.click(screen.getByRole('button', { name: 'Review all my positions' }));
    await waitFor(() => expect(api.askAboutPositions).toHaveBeenLastCalledWith(expect.stringMatching(/^Review all my positions/), null));
  });

  it('says why a question has no answer', async () => {
    api.getPositionQuestions.mockResolvedValue([{ ...answered, status: 'failed', positions: undefined, summary: undefined,
      error: 'Interrupted (the server restarted); ask again' }]);
    render(<PositionAsk choices={choices} rules={[]} onRulesChanged={vi.fn()} />);
    expect(await screen.findByText('Interrupted (the server restarted); ask again')).toBeInTheDocument();
  });
});
