import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AssistantPlans } from '../AssistantPlans';

const api = vi.hoisted(() => ({ parseAssistantPlan: vi.fn(), addAssistantPlan: vi.fn(), removeAssistantPlan: vi.fn() }));
vi.mock('../../../api/tradeDesk', () => ({ tradeDeskApi: api }));

const plans = [
  { id: 'you|SPY|plan|a', ticker: 'SPY', status: 'waiting', source: 'you', kind: 'plan', direction: 'long',
    trigger: { kind: 'close_above', price: 738.15, barMinutes: 5 }, stop: 734.58, target: 742, timeStop: '15:30' },
  { id: 'assistant|SPY|call|b', ticker: 'SPY', status: 'closed', source: 'assistant', kind: 'call', direction: 'short',
    trigger: { kind: 'now', price: null, barMinutes: null }, stop: 737, target: 733, timeStop: '16:00', price: 735, how: 'target',
    r: 1.95, twinR: -0.1, why: 'Lost VWAP on volume' },
];

describe('AssistantPlans', () => {
  afterEach(() => vi.clearAllMocks());

  it('reads a typed plan for you to check, then watches it once you confirm', async () => {
    api.parseAssistantPlan.mockResolvedValue({ draft: { direction: 'long', trigger: { kind: 'close_above', price: 738.15, barMinutes: 5 },
      invalidation: null, stop: 734.58, target: 742, timeStop: '15:30', instrument: 'calls' }, questions: ['Touch or close for the stop?'],
    text: 'x', model: 'Claude Code (claude-sonnet-5-5)', seconds: 6 });
    api.addAssistantPlan.mockResolvedValue(plans[0]);
    render(<AssistantPlans ticker="SPY" plans={[]} />);
    fireEvent.change(screen.getByLabelText('Your plan in words'), { target: { value: 'calls over VWAP on a 5 min close' } });
    fireEvent.click(screen.getByRole('button', { name: 'Read it' }));
    expect(await screen.findByText(/Read by Claude Code \(claude-sonnet-5-5\)/)).toBeInTheDocument();
    expect(screen.getByText('Touch or close for the stop?')).toBeInTheDocument();
    expect(screen.getByLabelText('Plan trigger price')).toHaveValue('738.15');
    fireEvent.change(screen.getByLabelText('Plan stop'), { target: { value: '735' } });
    fireEvent.click(screen.getByRole('button', { name: 'Watch this plan' }));
    await waitFor(() => expect(api.addAssistantPlan).toHaveBeenCalledWith(expect.objectContaining({
      ticker: 'SPY', direction: 'long', triggerKind: 'close_above', triggerPrice: 738.15, barMinutes: 5, stop: 735, target: 742,
      timeStop: '15:30', text: 'calls over VWAP on a 5 min close' })));
    await waitFor(() => expect(screen.queryByLabelText('Plan stop')).not.toBeInTheDocument());
  });

  it('lists plans and calls with their state and grade, and removes only a waiting one', async () => {
    api.removeAssistantPlan.mockResolvedValue(undefined);
    render(<AssistantPlans ticker="SPY" plans={plans as never} />);
    expect(screen.getByText(/Long a bar closes above 738.15 \(5-min bar\), stop 734.58, target 742.00, out by 15:30/)).toBeInTheDocument();
    expect(screen.getByText(/Assistant call \(untested model call\)/)).toBeInTheDocument();
    expect(screen.getByText(/target \+1.95R, random timing −0.10R/)).toBeInTheDocument();
    expect(screen.getAllByLabelText('Remove plan')).toHaveLength(1);
    fireEvent.click(screen.getByLabelText('Remove plan'));
    await waitFor(() => expect(api.removeAssistantPlan).toHaveBeenCalledWith('you|SPY|plan|a'));
  });

  it('can be filled in by hand and says what is missing', async () => {
    render(<AssistantPlans ticker="SPY" plans={[]} />);
    fireEvent.click(screen.getByRole('button', { name: 'Fill in by hand' }));
    fireEvent.click(screen.getByRole('button', { name: 'Watch this plan' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('A plan needs a direction, a stop and a target');
    expect(api.addAssistantPlan).not.toHaveBeenCalled();
  });
});
