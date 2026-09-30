import type { HoldingsView, TradeAdviceRequest, TradeAdviceStatus, TradeDeskDataMode, TradeDeskHealth, TradeFillIntent } from '../../types/tradeDesk';
import type { DecisionSignalItem } from '../../types/decisionSignals';

export const ARCHIVE_REASONS: Record<string, string> = { expired: 'options expired', stale: 'quotes stale', no_result: 'no result', old: 'over a week old' };

export type AdviceFormState = {
  ticker: string;
  dataMode: TradeDeskDataMode;
  direction: TradeAdviceRequest['direction'];
  horizon: TradeAdviceRequest['horizon'];
  expiry: string;
  allocation: string;
  existingShares: string;
  marginPerUnit: string;
  planLegs: string;
  feePerContract: string;
  riskFreeRate: string;
  dividendYield: string;
  message: string;
  useHoldings: boolean;
};

export type ManualFillState = {
  contractId: string;
  side: 'buy' | 'sell';
  quantity: string;
  price: string;
  fees: string;
  intent: TradeFillIntent;
  note: string;
  filledAt: string;
};

export const DEFAULT_FORM: AdviceFormState = {
  ticker: '',
  dataMode: 'live',
  direction: 'auto',
  horizon: 'both',
  expiry: '',
  allocation: '',
  existingShares: '0',
  marginPerUnit: '',
  planLegs: '',
  feePerContract: '0.65',
  riskFreeRate: '0',
  dividendYield: '0',
  message: '',
  useHoldings: true,
};

export const parseNumber = (value: string): number | undefined => {
  if (!value.trim()) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
};

// Fill quantities must be exact positive whole numbers; never coerce 0, 2.5 or text.
export const parseQuantity = (value: string): number | null => (/^\d+$/.test(value.trim()) && Number(value) >= 1 ? Number(value) : null);

export const parseInteger = (value: string, fallback = 0): number => {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : fallback;
};

export const formatMoney = (value: number | null | undefined): string => {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  return `$${Number(value).toFixed(2)}`;
};

export const formatNumber = (value: number | null | undefined, digits = 2): string => {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  return Number(value).toFixed(digits);
};

export const localDateTimeValue = (date = new Date()): string => new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);

export const formatDate = (value: string | null | undefined): string => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
};

export const formatPercent = (value: number | null | undefined): string => (
  value == null || !Number.isFinite(Number(value)) ? '—' : `${(Number(value) * 100).toFixed(1)}%`
);

export const signedPct =(value: number | null | undefined): string => (
  value == null || !Number.isFinite(Number(value)) ? '' : ` (${Number(value) >= 0 ? '+' : ''}${Number(value).toFixed(1)}%)`
);

/** What you hold in a ticker, from the broker sync: "200 shares (+4.2%); 35C 2026-10-02 (-18.0%)". */

export const heldSummary = (view: HoldingsView | null | undefined, ticker: string): string => {
  const symbol = ticker.trim().toUpperCase();
  if (!view || !symbol) return '';
  return [
    ...view.stocks.filter((row) => row.ticker === symbol).map((row) => `${row.qty} shares${signedPct(row.pnlPct)}`),
    ...view.options.filter((row) => row.underlying === symbol && !row.expired).map((row) => `${row.label} ${row.expiry}${signedPct(row.pnlPct)}`),
  ].join('; ');
};

export const VERDICT_LABELS: Record<string, string> = { buy: 'Buy', add: 'Add', hold: 'Hold', reduce: 'Reduce', sell: 'Sell', watch: 'Watch', avoid: 'Avoid', alert: 'Alert' };

/** The latest report's verdict on a stock: "Watch · score 45 · stop 31.20 · target 38.00 · Sep 29". */

export const verdictSummary = (item: DecisionSignalItem): string => {
  const created = item.createdAt ? new Date(item.createdAt) : null;
  return [
    VERDICT_LABELS[item.action] || item.action,
    item.score != null ? `score ${Math.round(item.score)}` : '',
    item.stopLoss ? `stop ${item.stopLoss.toFixed(2)}` : '',
    item.targetPrice ? `target ${item.targetPrice.toFixed(2)}` : '',
    created && !Number.isNaN(created.getTime()) ? created.toLocaleDateString([], { month: 'short', day: 'numeric' }) : '',
  ].filter(Boolean).join(' · ');
};

// An unavailable live provider blocks submission (no OpenD host/SDK, connection,
// login, permissions, quota) unless it is only "degraded", e.g. rights_unknown,
// where an authenticated quote attempt is still the way to verify access.
export const liveIsBlocked =(health: TradeDeskHealth | null): boolean => {
  if (!health || health.live.available) return false;
  return health.live.status !== 'degraded';
};

export const errorMessage = (error: unknown): string => {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === 'string') return error;
  if (error && typeof error === 'object' && 'message' in error) return String((error as { message?: unknown }).message || '');
  return 'Trade Desk request failed';
};

export const statusVariant = (status: TradeAdviceStatus): 'default' | 'success' | 'warning' | 'danger' | 'info' => {
  if (status === 'completed') return 'success';
  if (status === 'queued' || status === 'running') return 'info';
  if (status === 'stale') return 'warning';
  if (status === 'failed' || status === 'cancelled') return 'danger';
  return 'default';
};

export const statusLabel = (status: TradeAdviceStatus): string => {
  const labels: Record<string, string> = {
    queued: 'queued', running: 'running', completed: 'completed', stale: 'stale', failed: 'failed', cancelled: 'cancelled',
  };
  return labels[status] || status;
};

export const textValue = (value: unknown): string => {
  if (value == null) return '';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
  return Object.entries(value as Record<string, unknown>)
    .slice(0, 5)
    .map(([key, item]) => `${key}: ${typeof item === 'object' ? JSON.stringify(item) : String(item)}`)
    .join(' · ');
};

export const formatDetailValue = (key: string, value: unknown): string => {
  if (value == null || value === '') return '—';
  if (typeof value === 'number' && Number.isFinite(value)) {
    if (/(prob|percent|pct|rate|return|yield|change|move|volatility|confidence)/i.test(key)) {
      return Math.abs(value) <= 1 ? formatPercent(value) : `${value.toFixed(1)}%`;
    }
    if (/(price|pnl|profit|loss|debit|credit|premium|capital|fee|value|spot|strike|payoff|cost|amount)/i.test(key)) {
      return formatMoney(value);
    }
    return formatNumber(value);
  }
  if (typeof value === 'string' && /(at|time|timestamp|expiry|date)$/i.test(key)) {
    return formatDate(value);
  }
  return textValue(value);
};

export const formatQuoteAge = (value: string | null | undefined): string => {
  if (!value) return '—';
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return '—';
  const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60_000));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
};

export const modeLabel = (mode: TradeDeskDataMode, t: (key: 'tradeDesk.live' | 'tradeDesk.replay') => string): string => (
  mode === 'live' ? t('tradeDesk.live') : t('tradeDesk.replay')
);

export const shortDate = (value: string | null | undefined): string => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
};

/** Your questions grouped by stock, newest group first; the group holding the selection opens itself. */
