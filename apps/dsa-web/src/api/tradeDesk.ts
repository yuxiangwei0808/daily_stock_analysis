import apiClient from './index';
import { toCamelCase } from './utils';
import { API_BASE_URL } from '../utils/constants';
import type {
  CreateTradePlanRequest,
  HoldingRule,
  HoldingRuleDraft,
  HoldingRuleInput,
  HoldingsResponse,
  PaperFillRequest,
  TradeAdviceJob,
  TradeAdviceListResponse,
  TradeAdviceRequest,
  TradeDeskCatalogItem,
  TradeDeskHealth,
  TradeDeskPlan,
  TradeFillRequest,
  TradeJournalListResponse,
  TradeOutcomes,
  TradePositionListResponse,
  TradePreferences,
  TradePreferencesUpdate,
  TradeReconciliationRequest,
  UpdateTradePlanRequest,
} from '../types/tradeDesk';

const BASE_PATH = '/api/v1/trade-desk';

function withoutUndefined(values: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(values).filter(([, value]) => value !== undefined));
}

function toAdvicePayload(request: TradeAdviceRequest): Record<string, unknown> {
  return withoutUndefined({
    ticker: request.ticker.trim().toUpperCase(),
    allocation: request.allocation,
    direction: request.direction,
    horizon: request.horizon,
    data_mode: request.dataMode,
    expiry: request.expiry,
    strategies: request.strategies,
    message: request.message,
    parent_advice_id: request.parentAdviceId,
    source_report_id: request.sourceReportId,
    existing_shares: request.existingShares,
    fee_per_contract: request.feePerContract,
    risk_free_rate: request.riskFreeRate,
    dividend_yield: request.dividendYield,
    margin_per_unit: request.marginPerUnit,
    plan_legs: request.planLegs,
  });
}

function toPlanPayload(request: CreateTradePlanRequest): Record<string, unknown> {
  return withoutUndefined({
    advice_id: request.adviceId,
    candidate_id: request.candidateId,
    ledger: request.ledger,
    trigger_price: request.triggerPrice,
    trigger_direction: request.triggerDirection,
    invalidation_price: request.invalidationPrice,
    target_price: request.targetPrice,
    exit_at: request.exitAt,
  });
}

function toPlanUpdatePayload(request: UpdateTradePlanRequest): Record<string, unknown> {
  return withoutUndefined({
    monitoring: request.monitoring,
    status: request.status,
    notes: request.notes,
    trigger_price: request.triggerPrice,
    trigger_direction: request.triggerDirection,
    invalidation_price: request.invalidationPrice,
    target_price: request.targetPrice,
    exit_at: request.exitAt,
  });
}

function toPaperFillPayload(request: PaperFillRequest): Record<string, unknown> {
  return withoutUndefined({
    intent: request.intent,
    quantity: request.quantity,
    limit_price: request.limitPrice,
  });
}

function toFillPayload(request: TradeFillRequest): Record<string, unknown> {
  return {
    contract_id: request.contractId,
    side: request.side,
    quantity: request.quantity,
    price: request.price,
    fees: request.fees,
    filled_at: request.filledAt,
    intent: request.intent,
    note: request.note,
  };
}

function toPreferencesPayload(request: TradePreferencesUpdate): Record<string, unknown> {
  return withoutUndefined({
    proactive_enabled: request.proactiveEnabled,
    discord_enabled: request.discordEnabled,
    opportunity_daily_limit: request.opportunityDailyLimit,
    cooldown_minutes: request.cooldownMinutes,
  });
}

// These maps are keyed by server IDs (hex UUIDs). Deep key conversion would
// rewrite "3f2a9c..." as "3F2A9C...", so convert only their values.
const ID_KEYED_ADVICE_FIELDS: Record<string, string> = {
  triggers: 'triggers',
  snapshots: 'snapshots',
  source_snapshots: 'sourceSnapshots',
  effective_requests: 'effectiveRequests',
};

function adviceJobToCamelCase(data: unknown): TradeAdviceJob {
  if (!data || typeof data !== 'object') {
    return toCamelCase<TradeAdviceJob>(data);
  }
  const raw = data as Record<string, unknown>;
  const job = toCamelCase<Record<string, unknown>>(Object.fromEntries(
    Object.entries(raw).filter(([key]) => !(key in ID_KEYED_ADVICE_FIELDS)),
  ));
  for (const [key, camelKey] of Object.entries(ID_KEYED_ADVICE_FIELDS)) {
    if (!(key in raw)) {
      continue;
    }
    const value = raw[key];
    job[camelKey] = value && typeof value === 'object'
        ? Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([id, item]) => [id, toCamelCase(item)]))
        : value;
  }
  return job as unknown as TradeAdviceJob;
}

function unwrapJob(data: unknown): TradeAdviceJob {
  if (data && typeof data === 'object' && 'job' in data) {
    return adviceJobToCamelCase((data as { job: unknown }).job);
  }
  return adviceJobToCamelCase(data);
}

function unwrapPlan(data: unknown): TradeDeskPlan {
  if (data && typeof data === 'object' && 'plan' in data) {
    return toCamelCase<TradeDeskPlan>((data as { plan: unknown }).plan);
  }
  return toCamelCase<TradeDeskPlan>(data);
}

function unwrapPosition(data: unknown): TradePositionListResponse {
  if (data && typeof data === 'object' && 'position' in data) {
    return { items: [toCamelCase((data as { position: unknown }).position)] } as TradePositionListResponse;
  }
  if (data && typeof data === 'object' && ('planId' in data || 'plan_id' in data)) {
    return { items: [toCamelCase(data)] } as TradePositionListResponse;
  }
  return toCamelCase<TradePositionListResponse>(data);
}

export const tradeDeskApi = {
  async getHealth(): Promise<TradeDeskHealth> {
    const response = await apiClient.get<Record<string, unknown>>(`${BASE_PATH}/health`);
    return toCamelCase<TradeDeskHealth>(response.data);
  },

  async getCatalog(): Promise<{ items: TradeDeskCatalogItem[] }> {
    const response = await apiClient.get<Record<string, unknown>>(`${BASE_PATH}/catalog`);
    return toCamelCase<{ items: TradeDeskCatalogItem[] }>(response.data);
  },

  async createAdvice(request: TradeAdviceRequest): Promise<TradeAdviceJob> {
    const response = await apiClient.post<Record<string, unknown>>(`${BASE_PATH}/advice`, toAdvicePayload(request));
    return unwrapJob(response.data);
  },

  async listAdvice(): Promise<TradeAdviceListResponse> {
    const response = await apiClient.get<{ items?: unknown[] }>(`${BASE_PATH}/advice`);
    return { items: (response.data?.items ?? []).map(adviceJobToCamelCase) } as TradeAdviceListResponse;
  },

  async getAdvice(adviceId: string): Promise<TradeAdviceJob> {
    const response = await apiClient.get<Record<string, unknown>>(`${BASE_PATH}/advice/${encodeURIComponent(adviceId)}`);
    return unwrapJob(response.data);
  },

  async cancelAdvice(adviceId: string): Promise<TradeAdviceJob> {
    const response = await apiClient.post<Record<string, unknown>>(`${BASE_PATH}/advice/${encodeURIComponent(adviceId)}/cancel`);
    return unwrapJob(response.data);
  },

  async listPlans(): Promise<{ items: TradeDeskPlan[] }> {
    const response = await apiClient.get<Record<string, unknown>>(`${BASE_PATH}/plans`);
    return toCamelCase<{ items: TradeDeskPlan[] }>(response.data);
  },

  async createPlan(request: CreateTradePlanRequest): Promise<TradeDeskPlan> {
    const response = await apiClient.post<Record<string, unknown>>(`${BASE_PATH}/plans`, toPlanPayload(request));
    return unwrapPlan(response.data);
  },

  async updatePlan(planId: string, request: UpdateTradePlanRequest): Promise<TradeDeskPlan> {
    const response = await apiClient.patch<Record<string, unknown>>(`${BASE_PATH}/plans/${encodeURIComponent(planId)}`, toPlanUpdatePayload(request));
    return unwrapPlan(response.data);
  },

  async reconcilePlan(planId: string, request: TradeReconciliationRequest): Promise<TradePositionListResponse> {
    const response = await apiClient.post<Record<string, unknown>>(`${BASE_PATH}/plans/${encodeURIComponent(planId)}/reconcile`, request);
    return unwrapPosition(response.data);
  },

  async paperFill(planId: string, request: PaperFillRequest): Promise<TradePositionListResponse> {
    const response = await apiClient.post<Record<string, unknown>>(`${BASE_PATH}/plans/${encodeURIComponent(planId)}/paper-fill`, toPaperFillPayload(request));
    return unwrapPosition(response.data);
  },

  async paperSettle(planId: string, underlyingPrice: number): Promise<TradePositionListResponse> {
    const response = await apiClient.post<Record<string, unknown>>(`${BASE_PATH}/plans/${encodeURIComponent(planId)}/paper-settle`, { underlying_price: underlyingPrice });
    return unwrapPosition(response.data);
  },

  async createFill(planId: string, request: TradeFillRequest): Promise<TradePositionListResponse> {
    const response = await apiClient.post<Record<string, unknown>>(`${BASE_PATH}/plans/${encodeURIComponent(planId)}/fills`, toFillPayload(request));
    return unwrapPosition(response.data);
  },

  async listPositions(): Promise<TradePositionListResponse> {
    const response = await apiClient.get<Record<string, unknown>>(`${BASE_PATH}/positions`);
    return toCamelCase<TradePositionListResponse>(response.data);
  },

  async listJournal(): Promise<TradeJournalListResponse> {
    const response = await apiClient.get<Record<string, unknown>>(`${BASE_PATH}/journal`);
    return toCamelCase<TradeJournalListResponse>(response.data);
  },

  async getOutcomes(): Promise<TradeOutcomes> {
    const response = await apiClient.get<Record<string, unknown>>(`${BASE_PATH}/outcomes`);
    return toCamelCase<TradeOutcomes>(response.data);
  },

  async getPreferences(): Promise<TradePreferences> {
    const response = await apiClient.get<Record<string, unknown>>(`${BASE_PATH}/preferences`);
    return toCamelCase<TradePreferences>(response.data);
  },

  async updatePreferences(request: TradePreferencesUpdate): Promise<TradePreferences> {
    const response = await apiClient.patch<Record<string, unknown>>(`${BASE_PATH}/preferences`, toPreferencesPayload(request));
    return toCamelCase<TradePreferences>(response.data);
  },

  async getHoldings(): Promise<HoldingsResponse> {
    const response = await apiClient.get<Record<string, unknown>>(`${BASE_PATH}/holdings`);
    return toCamelCase<HoldingsResponse>(response.data);
  },

  async refreshHoldings(): Promise<HoldingsResponse> {
    const response = await apiClient.post<Record<string, unknown>>(`${BASE_PATH}/holdings/refresh`);
    return toCamelCase<HoldingsResponse>(response.data);
  },

  async createHoldingRule(rule: HoldingRuleInput): Promise<HoldingRule> {
    const response = await apiClient.post<Record<string, unknown>>(`${BASE_PATH}/holdings/rules`, withoutUndefined({
      position_key: rule.positionKey ?? undefined,
      ticker: rule.ticker,
      kind: rule.kind,
      value: rule.value,
      note: rule.note,
      repeat: rule.repeat,
    }));
    return toCamelCase<HoldingRule>(response.data);
  },

  async parseHoldingRule(text: string, positionKey?: string | null): Promise<HoldingRuleDraft> {
    const response = await apiClient.post<Record<string, unknown>>(`${BASE_PATH}/holdings/rules/parse`,
      withoutUndefined({ text, position_key: positionKey ?? undefined }));
    return toCamelCase<HoldingRuleDraft>(response.data);
  },

  async updateHoldingRule(ruleId: string, changes: { status?: 'active' | 'paused'; value?: number; note?: string }): Promise<HoldingRule> {
    const response = await apiClient.patch<Record<string, unknown>>(`${BASE_PATH}/holdings/rules/${encodeURIComponent(ruleId)}`, changes);
    return toCamelCase<HoldingRule>(response.data);
  },

  async deleteHoldingRule(ruleId: string): Promise<void> {
    await apiClient.delete(`${BASE_PATH}/holdings/rules/${encodeURIComponent(ruleId)}`);
  },

  getEventsUrl(after?: string): string {
    const query = after ? `?after=${encodeURIComponent(after)}` : '';
    return `${API_BASE_URL}${BASE_PATH}/events${query}`;
  },
};

export const TRADE_DESK_BASE_PATH = BASE_PATH;

/**
 * Return the US stock/ETF ticker Trade Desk accepts, or null for CN/HK codes,
 * market-review placeholders ("MARKET") and other non-optionable identifiers.
 */
export function tradeDeskTicker(code: unknown): string | null {
  const value = String(code ?? '').trim().toUpperCase().replace(/^US\./, '').replace(/\.US$/, '');
  return /^[A-Z]{1,5}(?:[.-][A-Z])?$/.test(value) ? value : null;
}
