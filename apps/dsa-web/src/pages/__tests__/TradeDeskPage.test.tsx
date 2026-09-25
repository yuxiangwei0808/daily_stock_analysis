import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import TradeDeskPage from '../TradeDeskPage';
import type { StrategyCandidate } from '../../types/tradeDesk';

const api = vi.hoisted(() => ({
  getHealth: vi.fn(),
  getCatalog: vi.fn(),
  createAdvice: vi.fn(),
  listAdvice: vi.fn(),
  getAdvice: vi.fn(),
  cancelAdvice: vi.fn(),
  listPlans: vi.fn(),
  createPlan: vi.fn(),
  updatePlan: vi.fn(),
  reconcilePlan: vi.fn(),
  paperFill: vi.fn(),
  paperSettle: vi.fn(),
  createFill: vi.fn(),
  listPositions: vi.fn(),
  listJournal: vi.fn(),
  getOutcomes: vi.fn(),
  getPreferences: vi.fn(),
  updatePreferences: vi.fn(),
  getEventsUrl: vi.fn(() => '/api/v1/trade-desk/events'),
}));

vi.mock('../../api/tradeDesk', () => ({ tradeDeskApi: api }));

vi.mock('recharts', () => {
  const Container = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  const Leaf = () => null;
  return {
    CartesianGrid: Leaf,
    Line: Leaf,
    LineChart: Container,
    ResponsiveContainer: Container,
    Tooltip: Leaf,
    XAxis: Leaf,
    YAxis: Leaf,
  };
});

const health = {
  enabled: true,
  live: { available: false, code: 'sdk_unavailable', message: 'Live quotes are unavailable.' },
  replay: { available: true, code: 'synthetic', message: 'Synthetic examples' },
  worker: { running: true },
  discordConfigured: false,
};

const baseRequest = {
  ticker: 'AAPL',
  direction: 'auto' as const,
  horizon: 'both' as const,
  dataMode: 'replay' as const,
  strategies: [],
  message: '',
  existingShares: 0,
  feePerContract: 0.65,
  riskFreeRate: 0,
  dividendYield: 0,
};

const queuedJob = {
  id: 'advice-queued',
  status: 'queued' as const,
  request: baseRequest,
  candidates: [],
  createdAt: '2026-09-22T10:00:00Z',
  updatedAt: '2026-09-22T10:00:00Z',
};

const candidate: StrategyCandidate = {
  id: 'candidate-put', strategy: 'long_put', title: 'Long put', underlying: 'AAPL',
  horizon: 'swing', snapshotId: 'snapshot-1',
  legs: [{ contractId: 'AAPL-P100', right: 'put', side: 'buy', quantity: 1, multiplier: 100,
    strike: 100, expiry: '2026-10-02T20:00:00Z', entryPrice: 2 }],
  payoff: { entryDebit: 200, fees: 0.65, maxLoss: 200.65, maxGain: 9799.35,
    gainBound: 'bounded', lossBound: 'bounded', breakevens: [97.9935],
    capitalRequired: 200.65, capitalNote: '', assignmentNote: '', points: [] },
  probability: { available: false, method: '', horizonLabel: '', reason: '', assumptions: {}, sensitivity: [] },
  scenarios: [], evidenceConfidence: 'low', reasons: [], warnings: [], entryConditions: [],
  invalidation: '', exitConditions: [], calculationVersion: 'test',
};

const cancelledJob = { ...queuedJob, status: 'cancelled' as const };

function setDefaultResponses() {
  api.getHealth.mockResolvedValue(health);
  api.getCatalog.mockResolvedValue({ items: [{ id: 'long_put', title: 'Long put' }] });
  api.listAdvice.mockResolvedValue({ items: [] });
  api.getAdvice.mockResolvedValue(queuedJob);
  api.cancelAdvice.mockResolvedValue(cancelledJob);
  api.listPlans.mockResolvedValue({ items: [] });
  api.listPositions.mockResolvedValue({ items: [] });
  api.listJournal.mockResolvedValue({ items: [] });
  api.getOutcomes.mockResolvedValue({
    paper: { closedTrades: 0, realizedPnl: 0, winRate: null },
    manualLive: { closedTrades: 0, realizedPnl: 0, winRate: null },
  });
  api.getPreferences.mockResolvedValue({
    proactiveEnabled: false,
    discordEnabled: false,
    opportunityDailyLimit: 3,
    cooldownMinutes: 60,
  });
  api.createAdvice.mockResolvedValue(queuedJob);
  api.createPlan.mockResolvedValue({});
  api.updatePlan.mockResolvedValue({});
  api.reconcilePlan.mockResolvedValue({ items: [] });
  api.paperFill.mockResolvedValue({ items: [] });
  api.paperSettle.mockResolvedValue({ items: [] });
  api.createFill.mockResolvedValue({ items: [] });
  api.updatePreferences.mockResolvedValue({
    proactiveEnabled: false,
    discordEnabled: false,
    opportunityDailyLimit: 3,
    cooldownMinutes: 60,
  });
}

function renderPage(entry = '/trade-desk') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <TradeDeskPage />
    </MemoryRouter>,
  );
}

describe('TradeDeskPage', () => {
  beforeEach(() => {
    Object.values(api).forEach((method) => {
      if (typeof method === 'function' && 'mockReset' in method) (method as ReturnType<typeof vi.fn>).mockReset();
    });
    setDefaultResponses();
    window.localStorage.clear();
  });

  it('hydrates a report deep link and clearly separates unavailable live from synthetic replay', async () => {
    renderPage('/trade-desk?ticker=AAPL&sourceReportId=42');

    expect(await screen.findByLabelText(/Ticker|股票代码/)).toHaveValue('AAPL');
    expect(screen.getByText('Report #42')).toBeInTheDocument();
    expect(screen.getAllByText(/synthetic examples|合成示例/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Live is currently unavailable|Live .*不可用/).length).toBeGreaterThan(0);
    expect(screen.getByText(/No account total is required|无需填写账户总值/)).toBeInTheDocument();
  });

  it('submits a replay request with the deep-link report provenance', async () => {
    renderPage('/trade-desk?ticker=AAPL&sourceReportId=42');
    await screen.findByLabelText(/Ticker|股票代码/);
    fireEvent.click(screen.getByRole('radio', { name: /Replay/ }));
    fireEvent.click(screen.getByRole('button', { name: /Generate opportunities|生成机会/ }));

    await waitFor(() => expect(api.createAdvice).toHaveBeenCalledWith(expect.objectContaining({
      ticker: 'AAPL',
      dataMode: 'replay',
      sourceReportId: 42,
    })));
  });

  it('renders the rollback-disabled state without advice controls', async () => {
    api.getHealth.mockResolvedValueOnce({ ...health, enabled: false });
    renderPage();

    expect(await screen.findByText(/Trade Desk is disabled by the server configuration|Trade Desk .*服务配置/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Generate opportunities|生成机会/ })).not.toBeInTheDocument();
  });

  it('cancels a queued advice job and keeps job lifecycle visible', async () => {
    api.listAdvice.mockResolvedValueOnce({ items: [queuedJob] });
    renderPage();

    const cancel = await screen.findByRole('button', { name: /Cancel job|取消任务/ });
    fireEvent.click(cancel);
    await waitFor(() => expect(api.cancelAdvice).toHaveBeenCalledWith('advice-queued'));
    expect((await screen.findAllByText(/cancelled|已取消/)).length).toBeGreaterThan(0);
  });

  it('polls an active advice job while it is queued', async () => {
    api.listAdvice.mockResolvedValueOnce({ items: [queuedJob] });
    renderPage();
    await screen.findByRole('button', { name: /Cancel job|取消任务/ });

    await new Promise((resolve) => window.setTimeout(resolve, 2_100));
    expect(api.getAdvice).toHaveBeenCalledWith('advice-queued');
  });

  it('continues from revised comparison inputs without changing the saved original', async () => {
    const job = { ...queuedJob, status: 'completed', candidates: [candidate],
      request: { ...baseRequest, allocation: 1500 },
      effectiveRequests: { [candidate.id]: { ...baseRequest, allocation: 500, direction: 'bearish' } } };
    api.listAdvice.mockResolvedValue({ items: [job] });
    renderPage();
    const followup = await screen.findByPlaceholderText(/Ask from this version|基于这个版本/);
    fireEvent.change(followup, { target: { value: 'Explain this choice further' } });
    fireEvent.click(screen.getByRole('button', { name: /Ask a follow-up|继续追问/ }));
    await waitFor(() => expect(api.createAdvice).toHaveBeenCalledWith(expect.objectContaining({
      allocation: 500, direction: 'bearish', parentAdviceId: job.id,
      message: 'Explain this choice further',
    })));
    expect(job.request.allocation).toBe(1500);
  });

  it('loads linked advice even when it is outside the latest advice list', async () => {
    api.listAdvice.mockResolvedValue({ items: [{ ...queuedJob, status: 'completed', explanation: 'Newest advice' }] });
    api.getAdvice.mockResolvedValue({ ...queuedJob, id: 'older-advice', status: 'completed', explanation: 'Linked saved advice' });
    renderPage('/trade-desk?adviceId=older-advice');
    expect((await screen.findAllByText('Linked saved advice')).length).toBeGreaterThan(0);
    expect(api.getAdvice).toHaveBeenCalledWith('older-advice');
    expect(screen.queryByText('Newest advice')).not.toBeInTheDocument();
  });

  it('opens and focuses the position referenced by a notification', async () => {
    api.listPositions.mockResolvedValue({ items: [{ planId: 'saved-plan', underlying: 'AAPL',
      ledger: 'paper', status: 'open', legs: [], realizedPnl: 0, fees: 0,
      unrealizedPnl: null, valuationStatus: 'unavailable' }] });
    renderPage('/trade-desk?planId=saved-plan');
    await waitFor(() => expect(document.getElementById('trade-plan-saved-plan')).toHaveFocus());
    expect(screen.getByRole('tab', { name: /Positions|持仓/ })).toHaveAttribute('aria-selected', 'true');
  });

  it('blocks live submission when OpenD is not configured but allows a degraded provider', async () => {
    api.getHealth.mockResolvedValue({ ...health, live: { available: false, status: 'blocked',
      code: 'opend_not_configured', message: 'Set TRADE_DESK_OPEND_HOST' } });
    renderPage('/trade-desk?ticker=AAPL');
    await screen.findByLabelText(/Ticker|股票代码/);
    await waitFor(() => expect(screen.getByRole('button', { name: /Generate opportunities|生成机会/ })).toBeDisabled());
  });

  it('allows live submission when rights are only unknown', async () => {
    api.getHealth.mockResolvedValue({ ...health, live: { available: false, status: 'degraded',
      code: 'rights_unknown', message: 'Rights not reported' } });
    renderPage('/trade-desk?ticker=AAPL');
    await screen.findByLabelText(/Ticker|股票代码/);
    await waitFor(() => expect(screen.getByRole('button', { name: /Generate opportunities|生成机会/ })).toBeEnabled());
  });

  it('labels replay alerts in the journal even when the payload has many fields', async () => {
    api.listJournal.mockResolvedValue({ items: [{ id: 7, eventType: 'price_trigger', createdAt: '2026-09-22T14:00:00Z',
      planId: 'plan-replay', payload: { planId: 'plan-replay', underlying: 'AAPL', price: 101, level: 100,
        message: 'Underlying price trigger reached', dataMode: 'replay', ledger: 'paper' } }] });
    renderPage('/trade-desk?view=journal');
    const row = (await screen.findByText('price_trigger')).parentElement as HTMLElement;
    expect(row.textContent).toMatch(/Replay/);
    expect(row.textContent).toMatch(/ledger/);
  });
  it('settles an expired paper position at the entered underlying price', async () => {
    api.listPositions.mockResolvedValue({ items: [{ planId: 'expired-plan', underlying: 'AAPL',
      ledger: 'paper', status: 'reconciliation_required', legs: [], realizedPnl: 0, fees: 0,
      unrealizedPnl: null, valuationStatus: 'unavailable' }] });
    renderPage('/trade-desk?view=positions');
    fireEvent.click(await screen.findByRole('button', { name: /Settle expiry/ }));
    expect(screen.queryByRole('button', { name: /^Reconcile$/ })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Underlying price at expiration'), { target: { value: '103' } });
    fireEvent.click(screen.getAllByRole('button', { name: /Settle expiry/ }).at(-1) as HTMLElement);
    await waitFor(() => expect(api.paperSettle).toHaveBeenCalledWith('expired-plan', 103));
  });

  it('rejects fractional or zero fill quantities instead of coercing them', async () => {
    api.listPositions.mockResolvedValue({ items: [{ planId: 'paper-plan', underlying: 'AAPL',
      ledger: 'paper', status: 'watching', legs: [], realizedPnl: 0, fees: 0,
      unrealizedPnl: null, valuationStatus: 'no_position' }] });
    renderPage('/trade-desk?view=positions');
    fireEvent.click(await screen.findByRole('button', { name: /Paper fill|模拟成交/ }));
    const quantity = screen.getAllByRole('spinbutton')[0];
    fireEvent.change(quantity, { target: { value: '2.5' } });
    // The open dialog renders before the position cards, so its submit comes first.
    fireEvent.click(screen.getAllByRole('button', { name: /Paper fill|模拟成交/ })[0]);
    expect(await screen.findByText(/whole-number quantity/)).toBeInTheDocument();
    expect(api.paperFill).not.toHaveBeenCalled();
  });
  it('offers to run a stale comparison again as a follow-up of the same conversation', async () => {
    const staleJob = { ...queuedJob, id: 'advice-stale', status: 'stale' as const,
      request: { ...baseRequest, dataMode: 'live' as const, message: 'Compare bullish spreads' }, candidates: [candidate] };
    api.listAdvice.mockResolvedValue({ items: [staleJob] });
    renderPage('/trade-desk');
    fireEvent.click(await screen.findByRole('button', { name: /Run again|重新运行/ }));
    await waitFor(() => expect(api.createAdvice).toHaveBeenCalledWith(expect.objectContaining({
      ticker: 'AAPL', dataMode: 'live', parentAdviceId: 'advice-stale', message: 'Compare bullish spreads',
    })));
  });
});
