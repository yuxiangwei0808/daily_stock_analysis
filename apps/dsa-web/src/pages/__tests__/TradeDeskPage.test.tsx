import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Link, MemoryRouter } from 'react-router-dom';
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
  repriceAdvice: vi.fn(),
  getTradeJournal: vi.fn(),
  refreshTradeJournal: vi.fn(),
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
  getTrackRecord: vi.fn(),
  deleteAdvice: vi.fn(),
  deleteArchivedAdvice: vi.fn(),
  getHoldings: vi.fn(),
  getPositionQuestions: vi.fn(),
  askAboutPositions: vi.fn(),
}));

vi.mock('../../api/tradeDesk', () => ({ tradeDeskApi: api }));

const signals = vi.hoisted(() => ({ getLatest: vi.fn() }));
vi.mock('../../api/decisionSignals', () => ({ decisionSignalsApi: signals }));

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
  api.getTrackRecord.mockResolvedValue({ windowDays: 90, groups: {}, recent: [] });
  api.getHoldings.mockResolvedValue({ enabled: false, view: null, rules: [] });
  api.getPositionQuestions.mockResolvedValue([]);
  signals.getLatest.mockResolvedValue({ items: [] });
  api.getAdvice.mockResolvedValue(queuedJob);
  api.cancelAdvice.mockResolvedValue(cancelledJob);
  api.listPlans.mockResolvedValue({ items: [] });
  api.listPositions.mockResolvedValue({ items: [] });
  api.listJournal.mockResolvedValue({ items: [] });
  api.getTradeJournal.mockResolvedValue({ journal: null });
  api.getOutcomes.mockResolvedValue({
    paper: { closedTrades: 0, realizedPnl: 0, winRate: null },
    manualLive: { closedTrades: 0, realizedPnl: 0, winRate: null },
  });
  api.getPreferences.mockResolvedValue({
    discordEnabled: false,
  });
  api.createAdvice.mockResolvedValue(queuedJob);
  api.createPlan.mockResolvedValue({});
  api.updatePlan.mockResolvedValue({});
  api.reconcilePlan.mockResolvedValue({ items: [] });
  api.paperFill.mockResolvedValue({ items: [] });
  api.paperSettle.mockResolvedValue({ items: [] });
  api.createFill.mockResolvedValue({ items: [] });
  api.updatePreferences.mockResolvedValue({
    discordEnabled: false,
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
    fireEvent.click(screen.getByRole('button', { name: /^(Ask|提问)$/ }));

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
    expect(screen.queryByRole('button', { name: /^(Ask|提问)$/ })).not.toBeInTheDocument();
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
    const followup = await screen.findByPlaceholderText(/Ask a follow-up about this answer|针对这个回答继续追问/);
    fireEvent.change(followup, { target: { value: 'Explain this choice further' } });
    fireEvent.click(screen.getByRole('button', { name: /Ask a follow-up|继续追问/ }));
    await waitFor(() => expect(api.createAdvice).toHaveBeenCalledWith(expect.objectContaining({
      allocation: 500, direction: 'bearish', parentAdviceId: job.id,
      message: 'Explain this choice further',
    })), { timeout: 5000 });
    expect(job.request.allocation).toBe(1500);
  });

  it('lists current requests first and loads the archive on demand', async () => {
    api.listAdvice.mockImplementation(async (scope?: string) => scope === 'archive'
      ? { items: [{ ...queuedJob, id: 'old-advice', status: 'completed', archived: 'expired', explanation: 'Expired advice' }], counts: { active: 1, archive: 1 } }
      : { items: [{ ...queuedJob, status: 'completed', explanation: 'Current advice' }], counts: { active: 1, archive: 1 } });
    renderPage();
    expect((await screen.findAllByText('Current advice')).length).toBeGreaterThan(0);
    expect(api.listAdvice).toHaveBeenCalledWith();
    fireEvent.click(await screen.findByRole('button', { name: 'Archive (1)' }));
    expect(await screen.findByText('options expired')).toBeInTheDocument();
    expect(api.listAdvice).toHaveBeenCalledWith('archive');
  });

  it('deletes one request after confirmation', async () => {
    api.listAdvice.mockResolvedValue({ items: [{ ...queuedJob, status: 'completed', explanation: 'Delete me' }], counts: { active: 1, archive: 0 } });
    api.deleteAdvice.mockResolvedValue({ deleted: [queuedJob.id] });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(`Delete ${queuedJob.request.ticker} request`) }));
    expect(api.deleteAdvice).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(api.deleteAdvice).toHaveBeenCalledWith(queuedJob.id));
    await waitFor(() => expect(screen.queryByText('Delete me')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Current (0)' })).toBeInTheDocument();
  });

  it('deletes the whole archive and reports kept requests', async () => {
    api.listAdvice.mockImplementation(async (scope?: string) => scope === 'archive'
      ? { items: [{ ...queuedJob, id: 'old-1', status: 'completed', archived: 'expired' }, { ...queuedJob, id: 'old-2', status: 'completed', archived: 'old' }], counts: { active: 0, archive: 2 } }
      : { items: [], counts: { active: 0, archive: 2 } });
    api.deleteArchivedAdvice.mockResolvedValue({ deleted: ['old-1'], kept: { 'old-2': 'has_plan' } });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Archive (2)' }));
    fireEvent.click(await screen.findByRole('button', { name: /Delete all archived/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    expect(await screen.findByText(/Deleted 1 archived requests; kept 1/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Archive (1)' })).toBeInTheDocument();
  });

  it('offers your broker position for a held ticker and sends the choice', async () => {
    api.getHoldings.mockResolvedValue({ enabled: true, rules: [], view: { stocks: [{ key: 'SOXS', ticker: 'SOXS', name: '', qty: 200, pnlPct: 4.2 }],
      options: [{ key: 'SOXS 2026-10-02', underlying: 'SOXS', expiry: '2026-10-02', daysLeft: 4, label: '35C', legs: [], cost: 100, pnlPct: -18 },
        { key: 'SOXS 2026-09-18', underlying: 'SOXS', expiry: '2026-09-18', daysLeft: 0, expired: true, label: '30C', legs: [], cost: 50 }] } });
    renderPage();
    const ticker = await screen.findByLabelText(/Ticker|股票代码|代码/);
    fireEvent.change(ticker, { target: { value: 'soxs' } });
    const hint = await screen.findByTestId('use-holdings');
    expect(hint).toHaveTextContent('You hold 200 shares (+4.2%); 35C 2026-10-02 (-18.0%).');
    expect(hint).not.toHaveTextContent('30C');
    fireEvent.click(screen.getByRole('checkbox', { name: /Use my position/ }));
    // Live is unavailable in this fixture; the choice is still sent with a replay request.
    fireEvent.click(screen.getByRole('radio', { name: /Replay/ }));
    expect(screen.queryByTestId('use-holdings')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^(Ask|提问)$/ }));
    await waitFor(() => expect(api.createAdvice).toHaveBeenCalledWith(expect.objectContaining({ ticker: 'SOXS', useHoldings: false })));
    fireEvent.change(ticker, { target: { value: 'AAPL' } });
    expect(screen.queryByTestId('use-holdings')).not.toBeInTheDocument();
  });

  it('shows the position an answer used', async () => {
    api.listAdvice.mockResolvedValue({ items: [{ ...queuedJob, status: 'completed', explanation: 'Hold or close',
      position: { ticker: 'SOXS', shares: 0, options: [{ expiry: '2026-10-02', label: '35C', daysLeft: 4, pnlPct: -18, legs: [] }],
        alerts: [{ kind: 'price_below', value: 30, status: 'active' }] },
      positionInputs: { existingShares: 0, planFromPosition: true } }] });
    renderPage();
    const panel = await screen.findByTestId('position-used');
    expect(panel).toHaveTextContent('35C 2026-10-02 (-18.0%) · 4 trading days left');
    expect(panel).toHaveTextContent('holding from here');
    expect(panel).toHaveTextContent('price below 30');
  });

  it('groups your questions by stock and keeps the ask panel folded when there is history', async () => {
    const job = (id: string, ticker: string, message: string) => ({ ...queuedJob, id, status: 'completed', explanation: `Answer ${id}`,
      request: { ...queuedJob.request, ticker, message } });
    api.listAdvice.mockResolvedValue({ items: [job('a1', 'SOXS', 'Day call entry?'), job('a2', 'NVDA', 'Covered call?'),
      job('a3', 'SOXS', 'Close or hold?'), job('a4', 'AAPL', 'Earnings play'), job('a5', 'META', 'Put spread')] });
    renderPage();
    const soxs = await screen.findByTestId('question-group-SOXS');
    expect(soxs).toHaveTextContent('Day call entry?');
    expect(soxs).toHaveTextContent('Close or hold?');
    // Only the newest group (and the selected question's) starts open.
    expect(screen.getByTestId('question-group-NVDA')).not.toHaveTextContent('Covered call?');
    fireEvent.click(screen.getByRole('button', { name: /NVDA/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Covered call\?/ }));
    expect((await screen.findAllByText('Answer a2')).length).toBeGreaterThan(0);
    // Filter narrows the groups.
    fireEvent.change(screen.getByLabelText(/Filter by ticker|按代码筛选/), { target: { value: 'me' } });
    expect(screen.queryByTestId('question-group-SOXS')).not.toBeInTheDocument();
    expect(screen.getByTestId('question-group-META')).toBeInTheDocument();
    // The ask panel is a dropdown: folded while history exists.
    expect(screen.queryByLabelText(/Ticker|股票代码/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Ask about a stock|问问股票/, expanded: false }));
    expect(await screen.findByLabelText(/Ticker|股票代码/)).toBeInTheDocument();
  });

  it('asks about a held position straight from the holdings tab', async () => {
    api.listAdvice.mockResolvedValue({ items: [{ ...queuedJob, status: 'completed', explanation: 'Older answer' }] });
    api.getHoldings.mockResolvedValue({ enabled: true, rules: [], view: { syncedAt: '2026-09-29T14:00:00Z', stocks: [{ key: 'SOXS', ticker: 'SOXS', name: 'Bear 3X', qty: 200, pnlPct: 4.2 }], options: [] } });
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: /Broker holdings|券商持仓|Holdings/ }));
    fireEvent.click((await screen.findAllByRole('button', { name: 'Ask about SOXS' }))[0]);
    // A holding's Ask opens the position question box on it; option strategies are one click away.
    expect(await screen.findByLabelText('Position')).toHaveValue('SOXS');
    fireEvent.click(screen.getByRole('button', { name: /Compare option strategies for SOXS/ }));
    expect(screen.getByRole('tab', { name: /Ask about a stock|问问股票/ })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByLabelText(/Ticker|股票代码/)).toHaveValue('SOXS');
    expect(await screen.findByTestId('use-holdings')).toHaveTextContent('You hold 200 shares');
  });

  it('shows the latest report verdict for the typed ticker', async () => {
    signals.getLatest.mockImplementation(async (ticker: string) => ticker === 'SOXS'
      ? { items: [{ id: 1, stockCode: 'SOXS', action: 'reduce', score: 32, stopLoss: 31.2, targetPrice: null,
        createdAt: '2026-09-29T13:45:00Z', reason: 'Semis rebounding; the inverse ETF loses its tailwind.' }] }
      : { items: [] });
    renderPage();
    const ticker = await screen.findByLabelText(/Ticker|股票代码/);
    fireEvent.change(ticker, { target: { value: 'soxs' } });
    const verdict = await screen.findByTestId('report-verdict', {}, { timeout: 2000 });
    expect(verdict).toHaveTextContent('Latest report: Reduce · score 32 · stop 31.20');
    expect(verdict).toHaveTextContent('Semis rebounding');
    expect(signals.getLatest).toHaveBeenCalledWith('SOXS', { market: 'us', limit: 1 });
    fireEvent.change(ticker, { target: { value: 'AAPL' } });
    await waitFor(() => expect(screen.queryByTestId('report-verdict')).not.toBeInTheDocument());
  });

  it('compares the candidates side by side, opens the pick and says shared caveats once', async () => {
    const common = 'Model probabilities are estimates.';
    const spread: StrategyCandidate = { ...candidate, id: 'candidate-spread', strategy: 'bear_put_debit', title: 'Bear put debit spread (swing)',
      payoff: { ...candidate.payoff, entryDebit: 150, maxGain: 350, maxLoss: 150.65, breakevens: [98.5] },
      reasons: ['Defined risk and cheaper than the long put.'], warnings: [common], entryConditions: ['Below 100 on volume'] };
    const longPut: StrategyCandidate = { ...candidate, warnings: [common, 'Short-dated: decays fast.'] };
    api.listAdvice.mockResolvedValue({ items: [{ ...queuedJob, status: 'completed', assessment: 'compare', explanation: 'The spread fits best.',
      candidates: [longPut, spread], triggers: { 'candidate-spread': { triggerPrice: 100, triggerDirection: 'below', invalidationPrice: 105 } } }] });
    renderPage();
    const table = await screen.findByTestId('answer-compare');
    expect(within(table).getAllByText('Long put').length).toBeGreaterThan(0);
    expect(within(table).getAllByText('$150.00 debit').length).toBeGreaterThan(0);
    const detail = screen.getByTestId('candidate-detail');  // the desk's pick opens first
    expect(detail).toHaveTextContent('Bear put debit spread');
    expect(detail).toHaveTextContent('Picked by the desk');
    expect(screen.getByTestId('candidate-why')).toHaveTextContent('Defined risk and cheaper than the long put.');
    expect(screen.getByTestId('candidate-levels')).toHaveTextContent('Enter below 100.00');
    expect(screen.getByTestId('candidate-levels')).toHaveTextContent('Wrong above 105.00');
    expect(screen.getByTestId('shared-notes')).toHaveTextContent(common);
    expect(detail).not.toHaveTextContent(common);  // said once, not on every candidate
    fireEvent.click(within(table).getAllByRole('button', { name: 'Long put' })[0]);
    expect(screen.getByTestId('candidate-detail')).toHaveTextContent('Short-dated: decays fast.');
    expect(screen.getByTestId('advice-verdict')).toHaveTextContent('The spread fits best.');
  });

  it('stars nothing when the desk says wait, but keeps its take on each strategy', async () => {
    const reviewed: StrategyCandidate = { ...candidate, reasons: ['Expires before your target can play out.'] };
    api.listAdvice.mockResolvedValue({ items: [{ ...queuedJob, status: 'completed', assessment: 'wait', explanation: 'Wait for a longer expiry.',
      candidates: [reviewed], triggers: { 'candidate-put': { exitAt: '2026-10-09T20:00:00Z' } } }] });
    renderPage();
    const detail = await screen.findByTestId('candidate-detail');
    expect(detail).not.toHaveTextContent('Picked by the desk');
    expect(screen.getByTestId('candidate-why')).toHaveTextContent('Expires before your target can play out.');
    expect(screen.getByTestId('candidate-levels')).toHaveTextContent('Exit by');
  });

  it('asks with Auto strategies unless you pick your own', async () => {
    api.getCatalog.mockResolvedValue({ items: [
      { id: 'long_put', title: 'Long put', category: 'directional' },
      { id: 'iron_condor', title: 'Iron condor', category: 'defined_risk' },
    ], defaults: { auto: ['long_put', 'iron_condor'] } });
    renderPage('/trade-desk?ticker=AAPL');
    expect(await screen.findByTestId('auto-strategies')).toHaveTextContent('Compares Long put, Iron condor.');
    expect(screen.queryByRole('button', { name: 'Iron condor' })).not.toBeInTheDocument();  // no list until you ask for it
    fireEvent.click(screen.getByRole('button', { name: 'Pick my own' }));
    const picker = screen.getByTestId('strategy-picker');
    expect(picker).toHaveTextContent('Range-bound');
    fireEvent.click(within(picker).getByRole('button', { name: 'Iron condor' }));
    fireEvent.click(within(picker).getByRole('button', { name: 'Auto (recommended)' }));  // back to Auto clears the pick
    expect(screen.getByTestId('auto-strategies')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Pick my own' }));
    expect(within(picker).getByRole('button', { name: 'Iron condor' })).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(within(picker).getByRole('button', { name: 'Iron condor' }));
    fireEvent.click(screen.getByRole('radio', { name: /Replay/ }));
    fireEvent.click(screen.getByRole('button', { name: /^(Ask|提问)$/ }));
    await waitFor(() => expect(api.createAdvice).toHaveBeenCalledWith(expect.objectContaining({ strategies: ['iron_condor'] })));
  });

  it('shows the NX tunnel an answer used', async () => {
    api.listAdvice.mockResolvedValue({ items: [{ ...queuedJob, status: 'completed', explanation: 'Use the fast tunnel as the stop',
      nxTunnel: { asOf: '2026-09-29', close: 33.26, fast: { top: 43.04, bottom: 40.01, state: 'below' }, slow: { top: 85.03, bottom: 77.83, state: 'below' },
        structure: 'fast_below_slow', changesToday: [], toFastBottomPct: -16.9, summary: 'Price 33.26: below the fast tunnel (40.01–43.04)' } }] });
    renderPage();
    const panel = await screen.findByTestId('nx-used');
    expect(panel).toHaveTextContent('Your NX tunnel');
    expect(panel).toHaveTextContent('as of 2026-09-29');
    expect(panel).toHaveTextContent('Price 33.26: below the fast tunnel (40.01–43.04)');
    expect(panel).toHaveTextContent('no tested edge');
  });

  it('shows the social and YouTube references an answer used', async () => {
    api.listAdvice.mockResolvedValue({ items: [{ ...queuedJob, status: 'completed', explanation: 'Crowded trade',
      references: [
        { kind: 'social_scan', title: 'Social attention on MU', summary: 'Reddit #1 by mentions (325, +81% vs a day earlier)' },
        { kind: 'youtube_picks', title: 'YouTube picks on MU, last 30 days', summary: 'Meet Kevin bullish (09-29)' },
      ] }] });
    renderPage();
    const panel = await screen.findByTestId('references-used');
    expect(panel).toHaveTextContent('Social attention on MU');
    expect(panel).toHaveTextContent('Reddit #1 by mentions');
    expect(panel).toHaveTextContent('Meet Kevin bullish (09-29)');
    expect(panel).toHaveTextContent('Background only');
  });

  it('keeps the answer you are reading when it leaves the current list', async () => {
    const reading = { ...queuedJob, id: 'reading', status: 'completed', explanation: 'The answer I am reading' };
    api.listAdvice.mockResolvedValueOnce({ items: [reading], counts: { active: 1, archive: 0 } })
      .mockResolvedValue({ items: [], counts: { active: 0, archive: 1 } });  // it went stale and was archived
    api.getAdvice.mockResolvedValue({ ...reading, status: 'stale' });
    renderPage();
    expect((await screen.findAllByText('The answer I am reading')).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByRole('button', { name: /^(Refresh|刷新)$/ })[0]);
    await waitFor(() => expect(api.getAdvice).toHaveBeenCalledWith('reading'));
    expect((await screen.findAllByText('The answer I am reading')).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Current (0)' })).toBeInTheDocument();  // not pushed into Current
  });

  it('drops a deleted linked answer without an error banner', async () => {
    api.getAdvice.mockRejectedValue(Object.assign(new Error('Advice not found'), { response: { status: 404 } }));
    renderPage('/trade-desk?adviceId=gone');
    await waitFor(() => expect(api.getAdvice).toHaveBeenCalledWith('gone'));
    expect(screen.queryByText('Advice not found')).not.toBeInTheDocument();
  });

  it('moves between tabs with the arrow keys and ignores unknown views', async () => {
    renderPage('/trade-desk?view=foo');
    const ask = await screen.findByRole('tab', { name: /Ask about a stock|问问股票/ });
    expect(ask).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'trade-desk-tab-opportunities');
    fireEvent.keyDown(ask, { key: 'ArrowRight' });
    const holdings = screen.getByRole('tab', { name: /Broker holdings|券商持仓|Holdings/ });
    expect(holdings).toHaveAttribute('aria-selected', 'true');
    expect(holdings).toHaveFocus();
    fireEvent.keyDown(holdings, { key: 'End' });
    expect(screen.getByRole('tab', { name: /^(Journal|日志)$/ })).toHaveAttribute('aria-selected', 'true');
  });

  it('opens the ask tab with the form unfolded for ?view=ask links', async () => {
    api.listAdvice.mockResolvedValue({ items: [{ ...queuedJob, status: 'completed', explanation: 'Older answer' }] });
    renderPage('/trade-desk?view=ask');
    const tab = await screen.findByRole('tab', { name: /Ask about a stock|问问股票/ });
    expect(tab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getAllByRole('tab')).toHaveLength(3);  // plan monitoring (Positions) was removed
    // Unfolded although there is history (the sidebar's "Ask" links here).
    expect(await screen.findByLabelText(/Ticker|股票代码/)).toBeInTheDocument();
  });

  it('opens the ask form when the sidebar Ask is clicked while already on the desk', async () => {
    api.listAdvice.mockResolvedValue({ items: [{ ...queuedJob, status: 'completed', explanation: 'Older answer' }] });
    render(
      <MemoryRouter initialEntries={['/trade-desk?view=journal']}>
        <Link to="/trade-desk?view=ask">Sidebar Ask</Link>
        <TradeDeskPage />
      </MemoryRouter>,
    );
    expect(await screen.findByRole('tab', { name: /^(Journal|日志)$/ })).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(screen.getByRole('link', { name: 'Sidebar Ask' }));
    expect(await screen.findByRole('tab', { name: /Ask about a stock|问问股票/ })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByLabelText(/Ticker|股票代码/)).toBeInTheDocument();
  });

  it('loads linked advice even when it is outside the latest advice list', async () => {
    api.listAdvice.mockResolvedValue({ items: [{ ...queuedJob, status: 'completed', explanation: 'Newest advice' }] });
    api.getAdvice.mockResolvedValue({ ...queuedJob, id: 'older-advice', status: 'completed', explanation: 'Linked saved advice' });
    renderPage('/trade-desk?adviceId=older-advice');
    expect((await screen.findAllByText('Linked saved advice')).length).toBeGreaterThan(0);
    expect(api.getAdvice).toHaveBeenCalledWith('older-advice');
    expect(screen.queryByText('Newest advice')).not.toBeInTheDocument();
  });

  it('blocks live submission when OpenD is not configured but allows a degraded provider', async () => {
    api.getHealth.mockResolvedValue({ ...health, live: { available: false, status: 'blocked',
      code: 'opend_not_configured', message: 'Set TRADE_DESK_OPEND_HOST' } });
    renderPage('/trade-desk?ticker=AAPL');
    await screen.findByLabelText(/Ticker|股票代码/);
    await waitFor(() => expect(screen.getByRole('button', { name: /^(Ask|提问)$/ })).toBeDisabled());
  });

  it('allows live submission when rights are only unknown', async () => {
    api.getHealth.mockResolvedValue({ ...health, live: { available: false, status: 'degraded',
      code: 'rights_unknown', message: 'Rights not reported' } });
    renderPage('/trade-desk?ticker=AAPL');
    await screen.findByLabelText(/Ticker|股票代码/);
    await waitFor(() => expect(screen.getByRole('button', { name: /^(Ask|提问)$/ })).toBeEnabled());
  });

  it('switches Discord categories off in the preferences', async () => {
    api.getPreferences.mockResolvedValue({ discordEnabled: true, discordCategories: { market: false } });
    renderPage('/trade-desk?view=journal');
    const ideas = await screen.findByRole('checkbox', { name: 'Trade ideas' });
    expect(screen.getByRole('checkbox', { name: 'Market moves & news' })).not.toBeChecked();
    expect(ideas).toBeChecked();
    fireEvent.click(ideas);
    fireEvent.click(screen.getByRole('button', { name: /^(Save settings|保存设置)$/ }));
    await waitFor(() => expect(api.updatePreferences).toHaveBeenCalledWith(
      expect.objectContaining({ discordEnabled: true, discordCategories: { market: false, ideas: false } })));
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

  it('refreshes the prices of a stale answer without asking the model again', async () => {
    const staleJob = { ...queuedJob, id: 'advice-stale', status: 'stale' as const,
      request: { ...baseRequest, dataMode: 'live' as const, message: 'Compare bullish spreads' }, candidates: [candidate] };
    api.listAdvice.mockResolvedValue({ items: [staleJob] });
    api.repriceAdvice.mockResolvedValue({ ...staleJob, repricedAt: '2026-09-30T15:02:00Z', repriceFailed: [] });
    renderPage('/trade-desk');
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh prices' }));
    await waitFor(() => expect(api.repriceAdvice).toHaveBeenCalledWith('advice-stale'));
    expect(await screen.findByTestId('reprice-bar')).toHaveTextContent('the explanation was written at the earlier prices');
    expect(api.createAdvice).not.toHaveBeenCalled();
  });

  it('keeps the warning when refreshed prices show the setup is invalidated', async () => {
    const staleJob = { ...queuedJob, id: 'advice-stale', status: 'stale' as const,
      request: { ...baseRequest, dataMode: 'live' as const, message: 'Compare bullish spreads' }, candidates: [candidate] };
    api.listAdvice.mockResolvedValue({ items: [staleJob] });
    api.repriceAdvice.mockResolvedValue({ ...staleJob, repricedAt: '2026-09-30T15:02:00Z', repriceFailed: [],
      invalidated: [candidate.id] });
    renderPage('/trade-desk');
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh prices' }));
    expect(await screen.findByText(/through a selected setup's invalidation level|越过所选方案的失效价位/)).toBeInTheDocument();
    expect(screen.queryByTestId('reprice-bar')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Run again|重新运行/ })).toBeInTheDocument();
  });

  it('says the reasoning no longer holds when the answer was invalidated as it was calculated', async () => {
    const staleJob = { ...queuedJob, id: 'advice-stale', status: 'stale' as const, invalidated: [candidate.id],
      request: { ...baseRequest, dataMode: 'live' as const, message: 'Compare bullish spreads' }, candidates: [candidate] };
    api.listAdvice.mockResolvedValue({ items: [staleJob] });
    renderPage('/trade-desk');
    expect(await screen.findByText(/through a selected setup's invalidation level|越过所选方案的失效价位/)).toBeInTheDocument();
  });
});
