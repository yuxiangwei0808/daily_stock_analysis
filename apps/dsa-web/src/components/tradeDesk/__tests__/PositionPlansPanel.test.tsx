import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PositionPlansPanel } from '../PositionPlansPanel';
import type { PositionPlansResponse } from '../../../types/tradeDesk';

const api = vi.hoisted(() => ({ getPositionPlans: vi.fn(), reviewPositions: vi.fn() }));
vi.mock('../../../api/tradeDesk', () => ({ tradeDeskApi: api }));

const plans: PositionPlansResponse = {
  enabled: true,
  review: { status: 'done', at: '2026-10-08T21:01:00Z', model: 'claude', summary: 'Trim SOFI.' },
  items: [
    { key: 'SOFI', ticker: 'SOFI', type: 'stock', exposure: 'long', label: 'SOFI shares', price: 15.18, dayPct: -2.4, pnlPct: -21,
      weightPct: 6.1, action: 'trim', stop: 15, stopSource: 'review', stopBasis: 'below the 20-day low', stopDistancePct: -1.19,
      target: 17.42, targetSource: 'review', targetBasis: 'the 50-day average', targetDistancePct: 14.76, status: 'near_stop',
      reason: 'Below both tunnels.', risk: 'Earnings Oct 27.' },
    { key: 'GOOG', ticker: 'GOOG', type: 'stock', exposure: 'long', label: 'GOOG shares', price: 348.9, pnlPct: 18.4, weightPct: 27.2,
      action: 'hold', stop: 341, stopSource: 'review', stopBasis: 'trailing: 2 ATR under the best close 349.10', stopDistancePct: -2.26,
      target: null, targetSource: '', status: 'ok' },
    { key: 'SQQQ 2026-10-16', ticker: 'SQQQ', type: 'option', exposure: 'long', label: 'SQQQ 10/16 35C', price: 32.07, pnlPct: -52,
      action: '', stop: 31.9, stopSource: 'you', stopDistancePct: -0.53, target: null, targetSource: '', status: 'stop_hit', daysLeft: 5 },
  ],
};

const renderPanel = () => render(<MemoryRouter><PositionPlansPanel /></MemoryRouter>);

describe('PositionPlansPanel', () => {
  beforeEach(() => {
    api.getPositionPlans.mockReset();
    api.reviewPositions.mockReset();
    window.localStorage.clear();
  });

  it('lists each position with its action, stop and target, and where each level came from', async () => {
    api.getPositionPlans.mockResolvedValue(plans);
    renderPanel();
    const panel = await screen.findByTestId('position-plans');
    expect(panel).toHaveTextContent('2 need attention');
    const sofi = within(panel).getByTestId('plan-row-SOFI');
    expect(sofi).toHaveTextContent('Near stop');
    expect(sofi).toHaveTextContent('Trim');
    expect(sofi).toHaveTextContent('15.00');
    expect(sofi).toHaveTextContent('1.2% away');
    expect(sofi).toHaveTextContent('17.42');
    expect(within(panel).getByTestId('plan-row-GOOG')).toHaveTextContent('trailing');
    const option = within(panel).getByTestId('plan-row-SQQQ 2026-10-16');
    expect(option).toHaveTextContent('Stop hit');
    expect(option).toHaveTextContent('your alert');
    expect(option).toHaveTextContent('levels on SQQQ');
    fireEvent.click(sofi);
    // The table and the phone cards both open the details (CSS shows one of them).
    expect((await within(panel).findAllByText('Below both tunnels.')).length).toBeGreaterThan(0);
    expect(within(panel).getAllByText('below the 20-day low').length).toBeGreaterThan(0);
    expect(screen.getByRole('link', { name: 'Holdings →' })).toHaveAttribute('href', '/trade-desk?view=holdings');
  });

  it('starts a review on request and stays out of the way without holdings', async () => {
    api.getPositionPlans.mockResolvedValue(plans);
    api.reviewPositions.mockResolvedValue(undefined);
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: /Review now/ }));
    await waitFor(() => expect(api.reviewPositions).toHaveBeenCalled());
    api.getPositionPlans.mockResolvedValue({ enabled: false, items: [], review: {} });
    const { container } = render(<MemoryRouter><PositionPlansPanel /></MemoryRouter>);
    await waitFor(() => expect(api.getPositionPlans).toHaveBeenCalledTimes(3));
    expect(within(container).queryByTestId('position-plans')).not.toBeInTheDocument();
  });

  it('folds away and remembers it', async () => {
    api.getPositionPlans.mockResolvedValue(plans);
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: /Your positions: plan/ }));
    expect(screen.queryByTestId('plan-row-SOFI')).not.toBeInTheDocument();
    expect(window.localStorage.getItem('dsa.positionPlans.collapsed')).toBe('1');
  });
});
