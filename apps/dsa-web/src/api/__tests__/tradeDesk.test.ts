import { beforeEach, describe, expect, it, vi } from 'vitest';
import { tradeDeskApi, tradeDeskTicker } from '../tradeDesk';

const { get, post, patch } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
}));

vi.mock('../index', () => ({
  default: { get, post, patch },
}));

describe('tradeDeskApi', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    patch.mockReset();
  });

  it('serializes advice requests and unwraps camel-cased jobs', async () => {
    post.mockResolvedValueOnce({
      data: {
        id: 'advice-1',
        status: 'queued',
        request: { ticker: 'AAPL', data_mode: 'replay', parent_advice_id: 'parent-1' },
        candidates: [],
        created_at: '2026-09-22T10:00:00Z',
        updated_at: '2026-09-22T10:00:00Z',
      },
    });

    const result = await tradeDeskApi.createAdvice({
      ticker: ' aapl ',
      allocation: 2500,
      direction: 'bearish',
      horizon: 'both',
      dataMode: 'replay',
      expiry: '2026-10-16',
      strategies: ['long_put'],
      message: 'Use a conservative debit.',
      parentAdviceId: 'parent-1',
      sourceReportId: 42,
      existingShares: 100,
      feePerContract: 0.65,
      riskFreeRate: 0.04,
      dividendYield: 0.01,
      marginPerUnit: 150,
    });

    expect(post).toHaveBeenCalledWith('/api/v1/trade-desk/advice', {
      ticker: 'AAPL',
      allocation: 2500,
      direction: 'bearish',
      horizon: 'both',
      data_mode: 'replay',
      expiry: '2026-10-16',
      strategies: ['long_put'],
      message: 'Use a conservative debit.',
      parent_advice_id: 'parent-1',
      source_report_id: 42,
      existing_shares: 100,
      fee_per_contract: 0.65,
      risk_free_rate: 0.04,
      dividend_yield: 0.01,
      margin_per_unit: 150,
    });
    expect(result.request.dataMode).toBe('replay');
  });

  it('omits untouched monitor overrides and normalizes direct position responses', async () => {
    post
      .mockResolvedValueOnce({ data: { id: 'plan-1', advice_id: 'advice-1', candidate_id: 'candidate-1' } })
      .mockResolvedValueOnce({
        data: {
          plan_id: 'plan-1',
          ledger: 'paper',
          underlying: 'AAPL',
          status: 'open',
          legs: [],
          realized_pnl: 0,
          unrealized_pnl: 0,
          valuation_status: 'pending',
          fees: 0,
        },
      });

    await tradeDeskApi.createPlan({
      adviceId: 'advice-1',
      candidateId: 'candidate-1',
      ledger: 'paper',
    });
    const position = await tradeDeskApi.paperFill('plan-1', { intent: 'open', quantity: 2 });

    expect(post).toHaveBeenNthCalledWith(1, '/api/v1/trade-desk/plans', {
      advice_id: 'advice-1',
      candidate_id: 'candidate-1',
      ledger: 'paper',
    });
    expect(position.items[0].planId).toBe('plan-1');
    expect(position.items[0].valuationStatus).toBe('pending');
    expect(post).toHaveBeenNthCalledWith(2, '/api/v1/trade-desk/plans/plan-1/paper-fill', {
      intent: 'open',
      quantity: 2,
    });
  });

  it('sends reconciliation notes, preferences, and persisted SSE cursor correctly', async () => {
    post.mockResolvedValueOnce({
      data: {
        position: {
          plan_id: 'plan-1',
          ledger: 'manual_live',
          underlying: 'AAPL',
          status: 'open',
          legs: [],
          realized_pnl: 0,
          unrealized_pnl: 0,
          valuation_status: 'broker_reconciled',
          fees: 0,
        },
      },
    });
    patch.mockResolvedValueOnce({
      data: {
        proactive_enabled: true,
        discord_enabled: false,
        opportunity_daily_limit: 5,
        cooldown_minutes: 30,
      },
    });

    const position = await tradeDeskApi.reconcilePlan('plan-1', { notes: 'Assignment recorded with broker statement.' });
    const preferences = await tradeDeskApi.updatePreferences({
      proactiveEnabled: true,
      discordEnabled: false,
      opportunityDailyLimit: 5,
      cooldownMinutes: 30,
    });

    expect(post).toHaveBeenCalledWith('/api/v1/trade-desk/plans/plan-1/reconcile', {
      notes: 'Assignment recorded with broker statement.',
    });
    expect(position.items[0].valuationStatus).toBe('broker_reconciled');
    expect(patch).toHaveBeenCalledWith('/api/v1/trade-desk/preferences', {
      proactive_enabled: true,
      discord_enabled: false,
      opportunity_daily_limit: 5,
      cooldown_minutes: 30,
    });
    expect(preferences.opportunityDailyLimit).toBe(5);
    expect(tradeDeskApi.getEventsUrl('journal-7')).toBe('/api/v1/trade-desk/events?after=journal-7');
  });
  it('keeps server ID keys intact in advice maps while camel-casing their values', async () => {
    const candidateId = '3f2a9cde0b1a4c5d8e7f6a5b4c3d2e1f';
    const snapshotId = '9a8b7c6d5e4f30211203948576abcdef';
    const job = {
      id: 'advice-2',
      status: 'completed',
      request: { ticker: 'AAPL', data_mode: 'live' },
      candidates: [{ id: candidateId, snapshot_id: snapshotId }],
      triggers: { [candidateId]: { trigger_price: 101, trigger_direction: 'below' } },
      snapshots: { [snapshotId]: { quoted_at: '2026-09-22T14:00:00Z' } },
      source_snapshots: { [snapshotId]: { quoted_at: '2026-09-22T13:59:00Z' } },
      effective_requests: { [candidateId]: { ticker: 'AAPL', existing_shares: 100 } },
      created_at: '2026-09-22T10:00:00Z',
      updated_at: '2026-09-22T10:00:00Z',
    };
    get.mockResolvedValueOnce({ data: job }).mockResolvedValueOnce({ data: { items: [job] } });

    for (const result of [await tradeDeskApi.getAdvice('advice-2'), (await tradeDeskApi.listAdvice()).items[0]]) {
      expect(result.candidates[0].snapshotId).toBe(snapshotId);
      expect(result.triggers?.[candidateId]).toEqual({ triggerPrice: 101, triggerDirection: 'below' });
      expect(result.snapshots?.[snapshotId]?.quotedAt).toBe('2026-09-22T14:00:00Z');
      expect((result.sourceSnapshots as Record<string, { quotedAt: string }>)[snapshotId].quotedAt).toBe('2026-09-22T13:59:00Z');
      expect(result.effectiveRequests?.[candidateId]?.existingShares).toBe(100);
    }
  });

  it('offers Trade Desk only for US stock and ETF tickers', () => {
    expect(tradeDeskTicker('aapl')).toBe('AAPL');
    expect(tradeDeskTicker('US.SPY')).toBe('SPY');
    expect(tradeDeskTicker('BRK.B')).toBe('BRK.B');
    expect(tradeDeskTicker('MARKET')).toBeNull();
    expect(tradeDeskTicker('HK00700')).toBeNull();
    expect(tradeDeskTicker('600519')).toBeNull();
  });
});
