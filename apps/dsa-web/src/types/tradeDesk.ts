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
  /** P/L if closed before expiry (same IVs): "now" at the quote time and "halfway" to expiry. */
  curves?: Array<{ label: 'now' | 'halfway' | string; at: string; points: PayoffPoint[] }>;
  /** Value change over the next day at today's price (negative: the position loses to time). */
  thetaPerDay?: number | null;
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
  /** Attach your broker position in this ticker (default true). */
  useHoldings?: boolean;
  /** Set by the server: "position" when plan legs are your held options. */
  planSource?: 'user' | 'position';
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
  /** Why the job is archived (expired, stale, no_result, old); absent while it is current. */
  archived?: string | null;
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
  /** When "Refresh prices" last re-priced the candidates (the explanation keeps its original prices). */
  repricedAt?: string | null;
  /** Candidates that could not be re-priced then (they keep their earlier numbers). */
  repriceFailed?: string[] | null;
  /** Selected candidates past their invalidation level or exit time at the latest prices (the answer is stale). */
  invalidated?: string[] | null;
  /** Your broker position in the ticker, attached to manual requests (read-only). */
  position?: TradeHeldPosition | null;
  positionInputs?: { existingShares: number; planFromPosition: boolean } | null;
  /** The user's NX tunnel on the ticker at the answer (daily; context and reference levels only). */
  nxTunnel?: {
    asOf: string;
    close: number;
    fast: { top: number; bottom: number; state: string };
    slow: { top: number; bottom: number; state: string };
    structure: string;
    changesToday: string[];
    toFastBottomPct: number;
    summary?: string;
  } | null;
  /** Social attention and the followed YouTube channels' picks at the answer (context only). */
  references?: { kind: 'social_scan' | 'youtube_picks' | string; title: string; summary: string }[] | null;
  [key: string]: unknown;
}

export interface TradeHeldPosition {
  ticker: string;
  syncedAt?: string | null;
  side?: string;
  shares: number;
  averageCost?: number | null;
  price?: number | null;
  stockPnlPct?: number | null;
  weightPct?: number | null;
  options: {
    expiry: string;
    label: string;
    daysLeft: number;
    pnlPct?: number | null;
    pctOfMax?: number | null;
    legs: { contract: string; right: string; strike: number; qty: number; averageCost?: number | null; mark?: number | null }[];
  }[];
  alerts: { kind: string; value: number; status: string; note?: string }[];
}

export type TradeAdviceScope = 'active' | 'archive' | 'all';

export interface TradeAdviceListResponse {
  items: TradeAdviceJob[];
  counts?: { active: number; archive: number };
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
  discordEnabled: boolean;
  /** Per-category switches (holdings, market, ideas, digest); a missing key means on. */
  discordCategories?: Record<string, boolean>;
  [key: string]: unknown;
}

export type TradePreferencesUpdate = Partial<Pick<TradePreferences, 'discordEnabled' | 'discordCategories'>>;

export interface TrackRecordGroup {
  label: string;
  open: number;
  closed: number;
  winRate?: number | null;
  avgReturnPct?: number | null;
  avgR?: number | null;
  avgVsSpyPct?: number | null;
}

export interface TrackRecordNxGroup {
  label: string;
  closed: number;
  open: number;
  winRate?: number | null;
  avgReturnPct?: number | null;
}

export interface TrackRecordVerdictCell {
  closed: number;
  open: number;
  avg5dPct?: number | null;
  avg10dPct?: number | null;
  avg10dVsSpyPct?: number | null;
}

export interface TrackRecord {
  windowDays: number;
  groups: Record<string, TrackRecordGroup>;
  /** Closed ideas and breakouts split by the NX slow tunnel at the signal: agree / neutral / against. */
  byNx?: Record<string, TrackRecordNxGroup>;
  /** The stock reports' calls (bullish / watch / bearish) by the NX slow tunnel at the report. */
  verdicts?: Record<string, { label: string; byNx: Record<string, TrackRecordVerdictCell> }>;
  recent: Array<{ ticker: string; direction: string; verdict: string; signalDay: string; status: string;
    reason?: string | null; returnPct?: number | null; kind?: string; nxAlignment?: string | null }>;
  /** Every tracked source vs SPY in the call's direction, with a cautious verdict. */
  scoreboard?: Array<{ key: string; label: string; horizon: string; closed: number; open: number;
    avgVsSpyPct?: number | null; t?: number | null; verdict: 'too_early' | 'ahead' | 'behind' | 'no_difference' | string }>;
}

export type HoldingRuleKind = 'price_below' | 'price_above' | 'days_to_expiry' | 'pnl_below' | 'pnl_above';

export interface HoldingStock {
  key: string;
  ticker: string;
  name: string;
  qty: number;
  averageCost?: number | null;
  price?: number | null;
  value?: number | null;
  weightPct?: number | null;
  pnlPct?: number | null;
}

export interface HoldingOptionLeg {
  code: string;
  right: 'call' | 'put';
  strike: number;
  qty: number;
  averageCost?: number | null;
  mark?: number | null;
}

export interface HoldingOption {
  key: string;
  underlying: string;
  expiry: string;
  daysLeft: number;
  expired?: boolean;
  label: string;
  legs: HoldingOptionLeg[];
  cost: number | null;
  value?: number | null;
  maxValue?: number | null;
  pnlPct?: number | null;
  pctOfMax?: number | null;
  underlyingPrice?: number | null;
  weightPct?: number | null;
}

export interface HoldingsView {
  account?: string | null;
  accountType?: string | null;
  syncedAt?: string | null;
  totalAssets?: number | null;
  cash?: number | null;
  stocks: HoldingStock[];
  options: HoldingOption[];
}

export interface HoldingRule {
  id: string;
  ticker: string;
  positionKey?: string | null;
  positionLabel?: string;
  kind: HoldingRuleKind;
  value: number;
  note?: string;
  repeat: 'once' | 'daily';
  status: 'active' | 'paused' | 'triggered';
  createdAt: string;
  triggeredAt?: string | null;
  warning?: string;
}

export type PositionAction = 'hold' | 'add' | 'trim' | 'take_profit' | 'close' | 'roll' | 'hedge' | 'review';

/** The model's take on one position: an action and levels (a stop/target the server checked against the price). */
export interface PositionAnswerItem {
  key: string;
  ticker: string;
  type: 'stock' | 'option';
  exposure: 'long' | 'short' | 'mixed';
  label: string;
  price?: number | null;
  pnlPct?: number | null;
  action: PositionAction;
  stop?: number | null;
  stopBasis?: string;
  stopSource?: 'model' | 'rule' | '';
  target?: number | null;
  targetBasis?: string;
  targetSource?: 'model' | 'rule' | '';
  pnlStopPct?: number | null;
  pnlTargetPct?: number | null;
  reason?: string;
  risk?: string;
}

export interface PositionQuestion {
  id: string;
  question: string;
  positionKey?: string | null;
  status: 'running' | 'done' | 'failed';
  createdAt: string;
  answeredAt?: string;
  model?: string;
  summary?: string;
  positions?: PositionAnswerItem[];
  error?: string;
}

export interface PortfolioSummary {
  date: string;
  message: string;
  builtAt?: string;
  dayPct?: number | null;
}

export interface HoldingsResponse {
  enabled: boolean;
  view: HoldingsView | null;
  rules: HoldingRule[];
  error?: string | null;
  summary?: PortfolioSummary | null;
}

export interface HoldingRuleInput {
  positionKey?: string | null;
  ticker?: string;
  kind: HoldingRuleKind;
  value: number;
  note?: string;
  repeat?: 'once' | 'daily';
}

export interface HoldingRuleDraft {
  kind: HoldingRuleKind;
  value: number;
  note?: string;
  positionKey?: string | null;
  source: 'pattern' | 'model';
}

export interface SystemStatusComponent {
  key: string;
  label: string;
  state: 'ok' | 'warn' | 'error' | 'off';
  detail: string;
  since?: string | null;
}

export interface SystemStatus {
  checkedAt: string;
  overall: 'ok' | 'warn' | 'error' | string;
  components: SystemStatusComponent[];
}

export interface PortfolioRiskRow {
  ticker: string;
  price?: number | null;
  sharesEquiv: number | null;
  deltaDollars?: number | null;
  thetaPerDay: number | null;
  thetaPartial: boolean;
  betaSpy?: number | null;
  betaQqq?: number | null;
  betaAssumed: boolean;
  betaQqqAssumed?: boolean;
  complete?: boolean;
}

export interface PortfolioRisk {
  asOf: string;
  complete?: boolean;
  unavailableTickers?: string[];
  rows: PortfolioRiskRow[];
  totals: { deltaDollars: number | null; spyBetaDollars: number | null; spyBetaPct?: number | null; thetaPerDay: number | null; thetaPct?: number | null };
  scenarios: Array<{ key: string; label: string; pnl: number | null; pct?: number | null }>;
}

export interface TradeJournalStats {
  trades: number;
  totalPnl: number;
  winRate?: number | null;
  avgPnl?: number | null;
  avgReturnPct?: number | null;
  avgHoldDays?: number | null;
}

export interface TradeJournalTrip {
  code: string;
  ticker: string;
  kind: 'stock' | 'call' | 'put' | string;
  position: 'long' | 'short' | string;
  opened: string;
  closed: string;
  pnl: number;
  returnPct?: number | null;
  how: string;
}

export interface TradeJournal {
  builtAt: string;
  fills: number;
  firstFill?: string | null;
  total: TradeJournalStats;
  byType: Array<TradeJournalStats & { label: string }>;
  byHold: Array<TradeJournalStats & { key: string; label: string }>;
  bySignal: Array<TradeJournalStats & { key: string; label: string }>;
  byUnderlying: Array<TradeJournalStats & { ticker: string }>;
  best: TradeJournalTrip[];
  worst: TradeJournalTrip[];
  openLotsNote: string;
  unresolved?: Array<{ code: string; reason: string; qty?: number; quantityDifference?: number; expiry?: string }>;
  /** Sales of positions held without a matching fill (bought before the history, or assigned): not counted. */
  unmatchedCloses?: number;
}
