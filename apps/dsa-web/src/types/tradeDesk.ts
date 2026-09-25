export type TradeDeskDataMode = 'live' | 'replay';
export type TradeDeskHorizon = 'intraday' | 'swing';
export type TradeAdviceHorizon = TradeDeskHorizon | 'both';
export type TradeAdviceDirection = 'auto' | 'bullish' | 'bearish' | 'neutral' | 'volatile';
export type TradeAdviceStatus = 'queued' | 'running' | 'completed' | 'stale' | 'failed' | 'cancelled' | string;
export type TradeLedger = 'paper' | 'manual_live';
export type TradePlanStatus = 'watching' | 'triggered' | 'filled' | 'closed' | 'invalidated' | 'archived' | string;
export type TradeFillIntent = 'open' | 'close' | 'assignment' | 'exercise';

export interface TradeDeskSourceStatus {
  available: boolean;
  code?: string;
  message?: string;
  status?: string;
  provider?: string;
  stale?: boolean;
  [key: string]: unknown;
}

export interface TradeDeskHealth {
  enabled: boolean;
  live: TradeDeskSourceStatus;
  replay: TradeDeskSourceStatus;
  worker: TradeDeskSourceStatus;
  discordConfigured: boolean;
  [key: string]: unknown;
}

export interface TradeDeskCatalogItem {
  id: string;
  title: string;
  description?: string;
  category?: string;
  horizon?: TradeDeskHorizon | string;
  dataMode?: TradeDeskDataMode | string;
  enabled?: boolean;
  [key: string]: unknown;
}

export interface OptionLeg {
  contractId: string;
  underlying?: string;
  right: 'call' | 'put' | 'stock' | string;
  side: 'buy' | 'sell' | string;
  quantity: number;
  strike?: number | null;
  expiry?: string | null;
  multiplier: number;
  entryPrice: number;
  iv?: number | null;
  existing?: boolean;
  exerciseStyle?: 'american' | 'european' | string;
}

export interface PayoffPoint {
  price: number;
  pnl: number;
  [key: string]: number;
}

export interface PayoffAnalysis {
  entryDebit: number;
  fees: number;
  maxGain?: number | null;
  maxLoss?: number | null;
  gainBound: 'bounded' | 'unbounded' | 'unknown' | string;
  lossBound: 'bounded' | 'unbounded' | 'unknown' | string;
  breakevens: number[];
  capitalRequired?: number | null;
  capitalNote: string;
  assignmentNote: string;
  points: PayoffPoint[];
}

export interface ProbabilityEstimate {
  available: boolean;
  probabilityOfProfit?: number | null;
  method: string;
  horizonAt?: string | null;
  horizonLabel: string;
  reason: string;
  assumptions: Record<string, unknown>;
  sensitivity: Array<Record<string, unknown>>;
}

export interface StrategyCandidate {
  id: string;
  strategy: string;
  title: string;
  underlying: string;
  horizon: TradeDeskHorizon;
  snapshotId: string;
  legs: OptionLeg[];
  payoff: PayoffAnalysis;
  probability: ProbabilityEstimate;
  scenarios: Array<Record<string, unknown>>;
  quantityForAllocation?: number | null;
  evidenceConfidence: 'low' | 'medium' | 'high' | string;
  reasons: string[];
  warnings: string[];
  entryConditions: string[];
  invalidation: string;
  exitConditions: string[];
  calculationVersion: string;
}

export interface TradePlanLeg {
  side: 'buy' | 'sell';
  right: 'call' | 'put' | 'stock';
  quantity: number;
  strike?: number | null;
  expiry?: string | null;
}

export interface TradeAdviceRequest {
  ticker: string;
  allocation?: number;
  direction: TradeAdviceDirection;
  horizon: TradeAdviceHorizon;
  dataMode: TradeDeskDataMode;
  expiry?: string;
  strategies: string[];
  message: string;
  parentAdviceId?: string;
  sourceReportId?: number;
  existingShares: number;
  feePerContract: number;
  riskFreeRate: number;
  dividendYield: number;
  marginPerUnit?: number;
  /** Lines like "buy 1 call 230 2026-10-16", or legs returned by the server. */
  planLegs?: string | TradePlanLeg[];
}

export interface TradeCandidateTrigger {
  triggerPrice?: number | null;
  triggerDirection?: 'above' | 'below' | null;
  invalidationPrice?: number | null;
  targetPrice?: number | null;
  exitAt?: string | null;
}

export interface TradeQuoteSnapshot {
  id?: string;
  underlying?: string;
  spot?: number;
  quotedAt?: string | null;
  receivedAt?: string | null;
  provider?: string;
  mode?: TradeDeskDataMode | string;
  session?: string;
  sourceVerified?: boolean;
  stale?: boolean;
  [key: string]: unknown;
}

export interface TradeModelOpinion {
  backend: string;
  model: string;
  role?: 'primary';
  status: 'ok' | 'error';
  action?: 'trade' | 'wait';
  candidateId?: string | null;
  strategy?: string | null;
  reason?: string;
  risk?: string;
  error?: string;
}

export interface TradeModelPanel {
  agreement: 'agree' | 'split' | 'unavailable';
  opinions: TradeModelOpinion[];
}

export interface TradeAdviceJob {
  id: string;
  status: TradeAdviceStatus;
  request: TradeAdviceRequest;
  candidates: StrategyCandidate[];
  assessment?: string | Record<string, unknown> | null;
  explanation?: string | Record<string, unknown> | null;
  error?: string | null;
  createdAt: string;
  updatedAt: string;
  snapshot?: TradeQuoteSnapshot | null;
  snapshots?: Record<string, TradeQuoteSnapshot> | null;
  parentAdviceId?: string | null;
  triggers?: Record<string, TradeCandidateTrigger> | null;
  usage?: Record<string, unknown> | null;
  effectiveRequests?: Record<string, TradeAdviceRequest> | null;
  panel?: TradeModelPanel | null;
  planError?: string | null;
  [key: string]: unknown;
}

export interface TradeAdviceListResponse {
  items: TradeAdviceJob[];
}

export interface TradeDeskPlan {
  id: string;
  adviceId: string;
  candidate: StrategyCandidate;
  candidateId?: string;
  ledger: TradeLedger;
  status: TradePlanStatus;
  createdAt: string;
  updatedAt: string;
  dataMode: TradeDeskDataMode;
  notes?: string;
  monitoring: boolean;
  triggerPrice?: number | null;
  triggerDirection: 'above' | 'below';
  invalidationPrice?: number | null;
  targetPrice?: number | null;
  exitAt?: string | null;
  [key: string]: unknown;
}

export interface CreateTradePlanRequest {
  adviceId: string;
  candidateId: string;
  ledger: TradeLedger;
  // null explicitly clears a level suggested by the saved advice.
  triggerPrice?: number | null;
  triggerDirection?: 'above' | 'below';
  invalidationPrice?: number | null;
  targetPrice?: number | null;
  exitAt?: string | null;
}

export interface UpdateTradePlanRequest {
  monitoring?: boolean;
  status?: 'invalidated' | 'archived';
  notes?: string;
  triggerPrice?: number | null;
  triggerDirection?: 'above' | 'below';
  invalidationPrice?: number | null;
  targetPrice?: number | null;
  exitAt?: string | null;
}

export interface TradeReconciliationRequest {
  notes: string;
}

export interface PaperFillRequest {
  intent: 'open' | 'close';
  quantity: number;
  limitPrice?: number;
}

export interface TradeFillRequest {
  contractId: string;
  side: 'buy' | 'sell';
  quantity: number;
  price: number;
  fees: number;
  filledAt: string;
  intent: TradeFillIntent;
  note: string;
}

export interface TradePositionLeg {
  contractId: string;
  right: 'call' | 'put' | 'stock' | string;
  quantity: number;
  signedQuantity: number;
  multiplier: number;
  averagePrice: number;
  markPrice?: number | null;
  unrealizedPnl?: number | null;
}

export interface TradePosition {
  planId: string;
  ledger: TradeLedger;
  underlying: string;
  status: string;
  legs: TradePositionLeg[];
  realizedPnl: number;
  unrealizedPnl: number;
  valuationStatus: string;
  valuationAt?: string | null;
  fees: number;
  plan?: TradeDeskPlan | null;
}

export interface TradePositionListResponse {
  items: TradePosition[];
}

export interface TradeJournalEvent {
  id: string | number;
  eventType: string;
  planId?: string | null;
  adviceId?: string | null;
  payload: Record<string, unknown>;
  createdAt: string;
}

export interface TradeJournalListResponse {
  items: TradeJournalEvent[];
}

export interface TradeOutcomeBucket {
  closedTrades: number;
  realizedPnl: number;
  winRate?: number | null;
  [key: string]: unknown;
}

export interface TradeOutcomes {
  paper: TradeOutcomeBucket;
  paperReplay?: TradeOutcomeBucket;
  manualLive: TradeOutcomeBucket;
  [key: string]: unknown;
}

export interface TradePreferences {
  proactiveEnabled: boolean;
  discordEnabled: boolean;
  opportunityDailyLimit: number;
  cooldownMinutes: number;
  [key: string]: unknown;
}

export type TradePreferencesUpdate = Partial<Pick<TradePreferences, 'proactiveEnabled' | 'discordEnabled' | 'opportunityDailyLimit' | 'cooldownMinutes'>>;
