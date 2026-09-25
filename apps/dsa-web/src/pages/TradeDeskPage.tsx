import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, BookOpen, Check, ChevronRight, CircleDollarSign, FileQuestion, LineChart as LineChartIcon, Pause, Play, RefreshCw, Settings2, ShieldCheck, SlidersHorizontal, Sparkles, X } from 'lucide-react';
import { Link, useSearchParams } from 'react-router-dom';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { tradeDeskApi } from '../api/tradeDesk';
import { focusPayoffPoints } from '../utils/payoff';
import { AppPage, Badge, Button, Card, EmptyState, InlineAlert, Loading, PageHeader } from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type {
  CreateTradePlanRequest,

  StrategyCandidate,
  TradeAdviceJob,
  TradeAdviceRequest,
  TradeAdviceStatus,
  TradeDeskCatalogItem,
  TradeDeskDataMode,
  TradeDeskHealth,
  TradeDeskPlan,
  TradeCandidateTrigger,
  TradeQuoteSnapshot,
  TradeFillIntent,
  TradeJournalEvent,
  TradeModelPanel,
  UpdateTradePlanRequest,
  TradeLedger,
  TradeOutcomes,
  TradePosition,
  TradePreferences,
} from '../types/tradeDesk';

type TradeDeskView = 'opportunities' | 'ask' | 'positions' | 'journal';

type AdviceFormState = {
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
};

type ManualFillState = {
  contractId: string;
  side: 'buy' | 'sell';
  quantity: string;
  price: string;
  fees: string;
  intent: TradeFillIntent;
  note: string;
  filledAt: string;
};

const DEFAULT_FORM: AdviceFormState = {
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
};

const DEFAULT_PREFERENCES: TradePreferences = {
  proactiveEnabled: false,
  discordEnabled: false,
  opportunityDailyLimit: 3,
  cooldownMinutes: 60,
};

const parseNumber = (value: string): number | undefined => {
  if (!value.trim()) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
};

// Fill quantities must be exact positive whole numbers; never coerce 0, 2.5 or text.
const parseQuantity = (value: string): number | null => (/^\d+$/.test(value.trim()) && Number(value) >= 1 ? Number(value) : null);

const parseInteger = (value: string, fallback = 0): number => {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : fallback;
};

const formatMoney = (value: number | null | undefined): string => {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  return `$${Number(value).toFixed(2)}`;
};

const formatNumber = (value: number | null | undefined, digits = 2): string => {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  return Number(value).toFixed(digits);
};

const localDateTimeValue = (date = new Date()): string => new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);

const formatDate = (value: string | null | undefined): string => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
};

const formatPercent = (value: number | null | undefined): string => (
  value == null || !Number.isFinite(Number(value)) ? '—' : `${(Number(value) * 100).toFixed(1)}%`
);

// An unavailable live provider blocks submission (no OpenD host/SDK, connection,
// login, permissions, quota) unless it is only "degraded", e.g. rights_unknown,
// where an authenticated quote attempt is still the way to verify access.
const liveIsBlocked = (health: TradeDeskHealth | null): boolean => {
  if (!health || health.live.available) return false;
  return health.live.status !== 'degraded';
};

const errorMessage = (error: unknown): string => {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === 'string') return error;
  if (error && typeof error === 'object' && 'message' in error) return String((error as { message?: unknown }).message || '');
  return 'Trade Desk request failed';
};

const statusVariant = (status: TradeAdviceStatus): 'default' | 'success' | 'warning' | 'danger' | 'info' => {
  if (status === 'completed') return 'success';
  if (status === 'queued' || status === 'running') return 'info';
  if (status === 'stale') return 'warning';
  if (status === 'failed' || status === 'cancelled') return 'danger';
  return 'default';
};

const statusLabel = (status: TradeAdviceStatus): string => {
  const labels: Record<string, string> = {
    queued: 'queued', running: 'running', completed: 'completed', stale: 'stale', failed: 'failed', cancelled: 'cancelled',
  };
  return labels[status] || status;
};

const textValue = (value: unknown): string => {
  if (value == null) return '';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
  return Object.entries(value as Record<string, unknown>)
    .slice(0, 5)
    .map(([key, item]) => `${key}: ${typeof item === 'object' ? JSON.stringify(item) : String(item)}`)
    .join(' · ');
};

const formatDetailValue = (key: string, value: unknown): string => {
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

const formatQuoteAge = (value: string | null | undefined): string => {
  if (!value) return '—';
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return '—';
  const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60_000));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
};

function MetricList({ values }: { values: Record<string, unknown> }) {
  const entries = Object.entries(values);
  if (!entries.length) return <span className="text-secondary-text">—</span>;
  return (
    <dl className="space-y-1">
      {entries.map(([key, value]) => (
        <div key={key} className="flex justify-between gap-3">
          <dt className="break-words">{key}</dt>
          <dd className="text-right text-foreground">{formatDetailValue(key, value)}</dd>
        </div>
      ))}
    </dl>
  );
}

const modeLabel = (mode: TradeDeskDataMode, t: (key: 'tradeDesk.live' | 'tradeDesk.replay') => string): string => (
  mode === 'live' ? t('tradeDesk.live') : t('tradeDesk.replay')
);

function ModeBadge({ mode }: { mode: TradeDeskDataMode }) {
  const { t } = useUiLanguage();
  return <Badge variant={mode === 'live' ? 'info' : 'history'}>{modeLabel(mode, t)}</Badge>;
}

function AdviceForm({
  form,
  setForm,
  catalog,
  health,
  selectedStrategies,
  setSelectedStrategies,
  onSubmit,
  isSubmitting,
  sourceReportId,
}: {
  form: AdviceFormState;
  setForm: React.Dispatch<React.SetStateAction<AdviceFormState>>;
  catalog: TradeDeskCatalogItem[];
  health: TradeDeskHealth | null;
  selectedStrategies: string[];
  setSelectedStrategies: React.Dispatch<React.SetStateAction<string[]>>;
  onSubmit: () => void;
  isSubmitting: boolean;
  sourceReportId?: number;
}) {
  const { t } = useUiLanguage();
  const disabled = !health?.enabled || isSubmitting;
  const update = <K extends keyof AdviceFormState>(key: K, value: AdviceFormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
  };
  const toggleStrategy = (id: string) => {
    setSelectedStrategies((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  };
  const liveUnavailable = form.dataMode === 'live' && liveIsBlocked(health);

  return (
    <Card variant="gradient" padding="md">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <span className="label-uppercase">{t('tradeDesk.ask')}</span>
          <h2 className="mt-1 text-lg font-semibold text-foreground">{t('tradeDesk.getAdvice')}</h2>
        </div>
        {sourceReportId ? <Badge variant="info">Report #{sourceReportId}</Badge> : null}
      </div>

      <div className="mb-4 grid gap-3 rounded-2xl border border-border/60 bg-card/40 p-3 text-sm md:grid-cols-2">
        <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-border/50 p-3">
          <input type="radio" name="trade-desk-mode" checked={form.dataMode === 'live'} onChange={() => update('dataMode', 'live')} />
          <span><strong className="block text-foreground">{t('tradeDesk.live')}</strong><span className="text-secondary-text">{health?.live.available ? (health.live.message || 'Fresh verified quotes') : (health?.live.code === 'rights_unknown' ? 'Connected; quote permissions are unverified' : t('tradeDesk.liveUnavailable'))}</span></span>
        </label>
        <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-border/50 p-3">
          <input type="radio" name="trade-desk-mode" checked={form.dataMode === 'replay'} onChange={() => update('dataMode', 'replay')} />
          <span><strong className="block text-foreground">{t('tradeDesk.replay')}</strong><span className="text-secondary-text">{t('tradeDesk.replaySynthetic')}</span></span>
        </label>
      </div>
      {liveUnavailable ? <InlineAlert variant="warning" title={t('tradeDesk.liveUnavailable')} message={health?.live.message || t('tradeDesk.staleData')} /> : null}
      {form.dataMode === 'replay' ? <InlineAlert className="mt-3" variant="info" message={t('tradeDesk.replaySynthetic')} /> : null}

      <div className="mt-4 grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <label className="lg:col-span-2"><span className="mb-2 block text-sm font-medium text-foreground">{t('tradeDesk.ticker')}</span><input aria-label={t('tradeDesk.ticker')} value={form.ticker} onChange={(event) => update('ticker', event.target.value.toUpperCase())} placeholder={t('tradeDesk.tickerPlaceholder')} className="input-surface input-focus-glow h-11 w-full rounded-xl border bg-transparent px-4 text-sm text-foreground" /></label>
        <label><span className="mb-2 block text-sm font-medium text-foreground">{t('tradeDesk.direction')}</span><select aria-label={t('tradeDesk.direction')} value={form.direction} onChange={(event) => update('direction', event.target.value as AdviceFormState['direction'])} className="input-surface h-11 w-full rounded-xl border px-3 text-sm text-foreground"><option value="auto">{t('tradeDesk.direction.auto')}</option><option value="bullish">{t('tradeDesk.direction.bullish')}</option><option value="bearish">{t('tradeDesk.direction.bearish')}</option><option value="neutral">{t('tradeDesk.direction.neutral')}</option><option value="volatile">{t('tradeDesk.direction.volatile')}</option></select></label>
        <label><span className="mb-2 block text-sm font-medium text-foreground">{t('tradeDesk.horizon')}</span><select aria-label={t('tradeDesk.horizon')} value={form.horizon} onChange={(event) => update('horizon', event.target.value as AdviceFormState['horizon'])} className="input-surface h-11 w-full rounded-xl border px-3 text-sm text-foreground"><option value="both">{t('tradeDesk.horizon.both')}</option><option value="intraday">{t('tradeDesk.horizon.intraday')}</option><option value="swing">{t('tradeDesk.horizon.swing')}</option></select></label>
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <label><span className="mb-2 block text-sm font-medium text-foreground">{t('tradeDesk.expiry')}</span><input aria-label={t('tradeDesk.expiry')} type="date" value={form.expiry} onChange={(event) => update('expiry', event.target.value)} className="input-surface h-11 w-full rounded-xl border px-3 text-sm text-foreground" /></label>
        <label><span className="mb-2 block text-sm font-medium text-foreground">{t('tradeDesk.allocation')}</span><input aria-label={t('tradeDesk.allocation')} type="number" min="0" step="any" value={form.allocation} onChange={(event) => update('allocation', event.target.value)} placeholder="optional" className="input-surface h-11 w-full rounded-xl border px-3 text-sm text-foreground" /></label>
        <label><span className="mb-2 block text-sm font-medium text-foreground">{t('tradeDesk.existingShares')}</span><input aria-label={t('tradeDesk.existingShares')} type="number" min="0" step="1" value={form.existingShares} onChange={(event) => update('existingShares', event.target.value)} className="input-surface h-11 w-full rounded-xl border px-3 text-sm text-foreground" /></label>
        <label><span className="mb-2 block text-sm font-medium text-foreground">{t('tradeDesk.marginPerUnit')}</span><input aria-label={t('tradeDesk.marginPerUnit')} type="number" min="0" step="any" value={form.marginPerUnit} onChange={(event) => update('marginPerUnit', event.target.value)} placeholder="optional" className="input-surface h-11 w-full rounded-xl border px-3 text-sm text-foreground" /></label>
      </div>

      <div className="mt-4 rounded-2xl border border-border/50 bg-card/30 p-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-foreground"><SlidersHorizontal className="h-4 w-4 text-cyan" />{t('tradeDesk.strategy')}</div>
        <div className="mt-3 flex flex-wrap gap-2">
          {catalog.length === 0 ? <span className="text-sm text-secondary-text">{t('tradeDesk.strategyAll')}</span> : catalog.map((item) => {
            const selected = selectedStrategies.includes(item.id);
            return <button key={item.id} type="button" onClick={() => toggleStrategy(item.id)} className={`rounded-full border px-3 py-1.5 text-xs transition ${selected ? 'border-cyan/50 bg-cyan/10 text-cyan' : 'border-border/60 text-secondary-text hover:text-foreground'}`} aria-pressed={selected}>{selected ? <Check className="mr-1 inline h-3 w-3" /> : null}{item.title || item.id}</button>;
          })}
        </div>
      </div>

      <details className="mt-4 rounded-2xl border border-border/50 bg-card/20 p-3">
        <summary className="cursor-pointer text-sm font-semibold text-foreground">{t('tradeDesk.advanced')}</summary>
        <div className="mt-3 grid gap-4 md:grid-cols-3">
          <label><span className="mb-2 block text-xs text-secondary-text">{t('tradeDesk.feePerContract')}</span><input aria-label={t('tradeDesk.feePerContract')} type="number" min="0" step="any" value={form.feePerContract} onChange={(event) => update('feePerContract', event.target.value)} className="input-surface h-10 w-full rounded-xl border px-3 text-sm text-foreground" /></label>
          <label><span className="mb-2 block text-xs text-secondary-text">{t('tradeDesk.riskFreeRate')}</span><input aria-label={t('tradeDesk.riskFreeRate')} type="number" step="any" value={form.riskFreeRate} onChange={(event) => update('riskFreeRate', event.target.value)} className="input-surface h-10 w-full rounded-xl border px-3 text-sm text-foreground" /></label>
          <label><span className="mb-2 block text-xs text-secondary-text">{t('tradeDesk.dividendYield')}</span><input aria-label={t('tradeDesk.dividendYield')} type="number" min="0" step="any" value={form.dividendYield} onChange={(event) => update('dividendYield', event.target.value)} className="input-surface h-10 w-full rounded-xl border px-3 text-sm text-foreground" /></label>
        </div>
      </details>

      <label className="mt-4 block"><span className="mb-2 block text-sm font-medium text-foreground">{t('tradeDesk.message')}</span><textarea aria-label={t('tradeDesk.message')} value={form.message} onChange={(event) => update('message', event.target.value)} placeholder={t('tradeDesk.messagePlaceholder')} rows={3} className="input-surface input-focus-glow w-full rounded-xl border px-4 py-3 text-sm text-foreground" /></label>
      <label className="mt-4 block"><span className="mb-2 block text-sm font-medium text-foreground">{t('tradeDesk.planLegs')}</span><textarea aria-label={t('tradeDesk.planLegs')} value={form.planLegs} onChange={(event) => update('planLegs', event.target.value)} placeholder={'buy 1 call 230 2026-10-16\nsell 1 call 240 2026-10-16'} rows={2} className="input-surface w-full rounded-xl border px-4 py-3 font-mono text-xs text-foreground" /><span className="mt-1 block text-xs text-secondary-text">{t('tradeDesk.planLegsHint')}</span></label>
      <div className="mt-4 flex flex-col gap-3 border-t border-border/50 pt-4 sm:flex-row sm:items-center sm:justify-between">
        <p className="max-w-2xl text-xs leading-5 text-secondary-text"><CircleDollarSign className="mr-1 inline h-3.5 w-3.5 text-cyan" />{t('tradeDesk.noAccountValue')}</p>
        <Button type="button" size="lg" disabled={disabled || liveUnavailable || !form.ticker.trim()} isLoading={isSubmitting} onClick={onSubmit}><Sparkles className="h-4 w-4" />{t('tradeDesk.getAdvice')}</Button>
      </div>
    </Card>
  );
}

function PayoffChart({ candidate }: { candidate: StrategyCandidate }) {
  const anchors = [
    ...candidate.legs.filter((leg) => leg.right !== 'stock').map((leg) => Number(leg.strike)),
    ...(candidate.payoff.breakevens || []).map(Number),
  ];
  const points = focusPayoffPoints(candidate.payoff.points || [], anchors);
  if (points.length < 2) return <p className="text-sm text-secondary-text">No payoff curve is available.</p>;
  return (
    <div className="h-48 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" opacity={0.5} />
          <XAxis
            type="number"
            dataKey="price"
            domain={['dataMin', 'dataMax']}
            tickFormatter={(value: number) => formatNumber(value, 0)}
            stroke="var(--muted-text)"
            tick={{ fill: 'var(--muted-text)' }}
            fontSize={10}
          />
          <YAxis
            tickFormatter={(value: number) => formatNumber(value, 0)}
            stroke="var(--muted-text)"
            tick={{ fill: 'var(--muted-text)' }}
            fontSize={10}
          />
          <Tooltip formatter={(value) => formatMoney(Number(value))} labelFormatter={(value) => `Underlying ${formatNumber(Number(value))}`} />
          <Line type="linear" dataKey="pnl" stroke="var(--color-cyan)" strokeWidth={2} dot={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function CandidateCard({ candidate, advice, onMonitor, monitoring }: { candidate: StrategyCandidate; advice: TradeAdviceJob; onMonitor: (request: CreateTradePlanRequest) => Promise<void>; monitoring: boolean }) {
  const { t } = useUiLanguage();
  const [ledger, setLedger] = useState<TradeLedger>('paper');
  const triggerDefaults = (advice.triggers?.[candidate.id] || {}) as TradeCandidateTrigger;
  const [triggerDirection, setTriggerDirection] = useState<'above' | 'below'>(triggerDefaults.triggerDirection === 'below' ? 'below' : 'above');
  const [triggerPrice, setTriggerPrice] = useState(triggerDefaults.triggerPrice == null ? '' : String(triggerDefaults.triggerPrice));
  const [targetPrice, setTargetPrice] = useState(triggerDefaults.targetPrice == null ? '' : String(triggerDefaults.targetPrice));
  const [invalidationPrice, setInvalidationPrice] = useState(triggerDefaults.invalidationPrice == null ? '' : String(triggerDefaults.invalidationPrice));
  const [exitAt, setExitAt] = useState(triggerDefaults.exitAt ? localDateTimeValue(new Date(triggerDefaults.exitAt)) : '');
  const payoff = candidate.payoff;
  const probability = candidate.probability;
  const replay = advice.request.dataMode === 'replay';
  const snapshot: TradeQuoteSnapshot | null = advice.snapshots?.[candidate.snapshotId] || advice.snapshot || null;
  const quoteTime = snapshot?.quotedAt || snapshot?.receivedAt || null;
  const sensitivity = probability.sensitivity || [];
  return (
    <Card variant="bordered" padding="md" className="overflow-hidden">
      <div className="flex flex-col gap-3 border-b border-border/50 pb-4 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-lg font-semibold text-foreground">{candidate.title}</h3>
            <ModeBadge mode={advice.request.dataMode} />
            <Badge variant={candidate.evidenceConfidence === 'high' ? 'success' : candidate.evidenceConfidence === 'medium' ? 'warning' : 'default'}>{t('tradeDesk.confidence')}: {candidate.evidenceConfidence}</Badge>
          </div>
          <p className="mt-1 text-sm text-secondary-text">{candidate.strategy} · {candidate.underlying} · {candidate.horizon}</p>
          <p className="mt-1 text-xs text-muted-text">
            {t('tradeDesk.snapshot')}: {formatDate(quoteTime)} · {t('tradeDesk.quoteAge')}: {formatQuoteAge(quoteTime)}
            {snapshot?.provider ? ` · ${snapshot.provider}` : ''}{snapshot?.stale ? ' · stale' : ''}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select aria-label={`${t('tradeDesk.ledger')} ${candidate.title}`} value={ledger} onChange={(event) => setLedger(event.target.value as TradeLedger)} className="input-surface h-9 rounded-lg border px-2 text-xs text-foreground">
            <option value="paper">{t('tradeDesk.paper')}</option>
            <option value="manual_live" disabled={replay}>{t('tradeDesk.manualLive')}{replay ? ' · disabled' : ''}</option>
          </select>
          <Button size="sm" variant="outline" disabled={monitoring || advice.status !== 'completed' || (ledger === 'manual_live' && replay)} onClick={() => {
            // Send every displayed level, including cleared ones, so the saved
            // plan never keeps an advice default the user did not see.
            const request: CreateTradePlanRequest = {
              adviceId: advice.id,
              candidateId: candidate.id,
              ledger,
              triggerPrice: parseNumber(triggerPrice) ?? null,
              triggerDirection,
              invalidationPrice: parseNumber(invalidationPrice) ?? null,
              targetPrice: parseNumber(targetPrice) ?? null,
              exitAt: exitAt ? new Date(exitAt).toISOString() : null,
            };
            void onMonitor(request);
          }}>
            {monitoring ? <Check className="h-4 w-4" /> : <ShieldCheck className="h-4 w-4" />}{monitoring ? t('tradeDesk.monitoring') : t('tradeDesk.monitor')}
          </Button>
        </div>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <h4 className="text-xs font-semibold uppercase tracking-[0.16em] text-secondary-text">{t('tradeDesk.legs')}</h4>
          <div className="mt-2 overflow-x-auto rounded-xl border border-border/50">
            <table className="w-full min-w-[620px] text-left text-xs">
              <thead className="bg-elevated/50 text-secondary-text"><tr><th className="px-3 py-2">{t('tradeDesk.contract')}</th><th className="px-3 py-2">{t('tradeDesk.side')}</th><th className="px-3 py-2">{t('tradeDesk.quantity')}</th><th className="px-3 py-2">Strike</th><th className="px-3 py-2">Expiry</th><th className="px-3 py-2">Entry</th></tr></thead>
              <tbody>{candidate.legs.map((leg) => <tr key={`${leg.contractId}-${leg.side}`} className="border-t border-border/40"><td className="px-3 py-2 font-mono text-foreground">{leg.contractId}<span className="ml-2 text-secondary-text">{leg.right}</span></td><td className="px-3 py-2 text-foreground">{leg.side}</td><td className="px-3 py-2 text-foreground">{leg.quantity} × {leg.multiplier}</td><td className="px-3 py-2 text-foreground">{formatNumber(leg.strike)}</td><td className="px-3 py-2 text-secondary-text">{leg.expiry?.slice(0, 10) || '—'}</td><td className="px-3 py-2 font-mono text-foreground">{formatMoney(leg.entryPrice)}</td></tr>)}</tbody>
            </table>
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <div className="rounded-xl bg-elevated/50 p-3"><span className="text-xs text-secondary-text">{t('tradeDesk.cost')}</span><div className="mt-1 text-sm text-foreground">{payoff.entryDebit < 0 ? t('tradeDesk.credit') : t('tradeDesk.debit')} {formatMoney(Math.abs(payoff.entryDebit))} · Fees {formatMoney(payoff.fees)}</div><div className="mt-1 text-xs text-secondary-text">Capital {formatMoney(payoff.capitalRequired)} · {payoff.capitalNote || '—'}</div>{candidate.quantityForAllocation != null ? <div className="mt-1 text-xs text-secondary-text">{t('tradeDesk.quantityEstimate')}: {candidate.quantityForAllocation}</div> : null}</div>
            <div className="rounded-xl bg-elevated/50 p-3"><span className="text-xs text-secondary-text">Breakevens</span><div className="mt-1 text-sm text-foreground">{payoff.breakevens.length ? payoff.breakevens.map((value) => formatMoney(value)).join(' · ') : '—'}</div><div className="mt-1 text-xs text-secondary-text">Assignment: {payoff.assignmentNote || '—'}</div></div>
          </div>
        </div>
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-[0.16em] text-secondary-text">{t('tradeDesk.payoff')}</h4>
          <div className="mt-2 rounded-xl border border-border/50 bg-card/30 p-2"><PayoffChart candidate={candidate} /></div>
          <div className="mt-3 grid grid-cols-2 gap-2 text-xs"><div className="rounded-xl bg-elevated/50 p-3"><span className="text-secondary-text">Max gain</span><strong className="mt-1 block text-foreground">{payoff.gainBound === 'unbounded' ? t('tradeDesk.unlimited') : payoff.gainBound === 'unknown' ? t('tradeDesk.unknown') : formatMoney(payoff.maxGain)}</strong></div><div className="rounded-xl bg-elevated/50 p-3"><span className="text-secondary-text">Max loss</span><strong className="mt-1 block text-foreground">{payoff.lossBound === 'unbounded' ? t('tradeDesk.unlimited') : payoff.lossBound === 'unknown' ? t('tradeDesk.unknown') : formatMoney(payoff.maxLoss)}</strong></div></div>
        </div>
      </div>

      <div className="mt-4 grid gap-4 border-t border-border/50 pt-4 md:grid-cols-3">
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-[0.16em] text-secondary-text">{t('tradeDesk.probability')}</h4>
          <p className="mt-2 text-2xl font-semibold text-foreground">{probability.available ? formatPercent(probability.probabilityOfProfit) : t('tradeDesk.unavailable')}</p>
          <p className="mt-1 text-xs leading-5 text-secondary-text">{probability.reason || probability.method} · {probability.horizonLabel}</p>
          {probability.horizonAt ? <p className="mt-1 text-xs text-muted-text">{t('tradeDesk.horizonAt')}: {formatDate(probability.horizonAt)}</p> : null}
          <div className="mt-2 max-h-40 overflow-y-auto rounded-lg bg-elevated/40 p-2 text-xs text-secondary-text"><p className="mb-1 font-semibold text-foreground">{t('tradeDesk.assumptions')}</p><MetricList values={probability.assumptions || {}} /></div>
          {sensitivity.length ? <details className="mt-2 rounded-lg border border-border/40 p-2" open><summary className="cursor-pointer text-xs font-semibold text-foreground">{t('tradeDesk.sensitivity')} ({sensitivity.length})</summary><div className="mt-2 max-h-36 space-y-2 overflow-y-auto text-xs text-secondary-text">{sensitivity.map((item, index) => <div key={`${candidate.id}-sensitivity-${index}`} className="rounded-lg bg-elevated/40 p-2"><MetricList values={item} /></div>)}</div></details> : null}
        </div>
        <div>
          <details className="rounded-lg border border-border/40 p-2" open>
            <summary className="cursor-pointer text-xs font-semibold text-foreground">{t('tradeDesk.scenarios')} ({candidate.scenarios.length})</summary>
            <div className="mt-2 max-h-56 space-y-2 overflow-y-auto pr-1">{candidate.scenarios.length ? candidate.scenarios.map((scenario, index) => {
              const label = textValue(scenario.label || scenario.name) || `${t('tradeDesk.scenarios')} ${index + 1}`;
              const details = Object.fromEntries(Object.entries(scenario).filter(([key]) => key !== 'label' && key !== 'name'));
              return <div key={`${candidate.id}-scenario-${index}`} className="rounded-xl bg-elevated/50 p-2 text-xs text-secondary-text"><p className="mb-1 font-semibold text-foreground">{label}</p><MetricList values={details} /></div>;
            }) : <p className="text-sm text-secondary-text">—</p>}</div>
          </details>
        </div>
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-[0.16em] text-secondary-text">{t('tradeDesk.warnings')}</h4>
          {candidate.warnings.length ? <ul className="mt-2 space-y-2 text-xs leading-5 text-warning">{candidate.warnings.map((warning) => <li key={warning}><AlertTriangle className="mr-1 inline h-3.5 w-3.5" />{warning}</li>)}</ul> : <p className="mt-2 text-sm text-secondary-text">No additional warnings.</p>}
          {candidate.invalidation ? <p className="mt-3 text-xs leading-5 text-secondary-text"><strong className="text-foreground">Invalidation:</strong> {candidate.invalidation}</p> : null}
        </div>
      </div>

      <div className="mt-4 grid gap-4 border-t border-border/50 pt-4 md:grid-cols-2">
        <div><h4 className="text-xs font-semibold uppercase tracking-[0.16em] text-secondary-text">{t('tradeDesk.entryConditions')}</h4>{candidate.entryConditions.length ? <ul className="mt-2 space-y-1 text-sm text-secondary-text">{candidate.entryConditions.map((condition) => <li key={condition}>• {condition}</li>)}</ul> : <p className="mt-2 text-sm text-secondary-text">{t('tradeDesk.noConditions')}</p>}</div>
        <div><h4 className="text-xs font-semibold uppercase tracking-[0.16em] text-secondary-text">{t('tradeDesk.exitConditions')}</h4>{candidate.exitConditions.length ? <ul className="mt-2 space-y-1 text-sm text-secondary-text">{candidate.exitConditions.map((condition) => <li key={condition}>• {condition}</li>)}</ul> : <p className="mt-2 text-sm text-secondary-text">{t('tradeDesk.noConditions')}</p>}</div>
      </div>

      <div className="mt-4 grid gap-3 rounded-xl border border-border/40 bg-card/30 p-3 sm:grid-cols-3 lg:grid-cols-5"><label className="text-xs text-secondary-text">Trigger price<input value={triggerPrice} onChange={(event) => setTriggerPrice(event.target.value)} type="number" step="any" placeholder="optional" className="input-surface mt-1 h-9 w-full rounded-lg border px-2 text-xs text-foreground" /></label><label className="text-xs text-secondary-text">Trigger direction<select value={triggerDirection} onChange={(event) => setTriggerDirection(event.target.value as 'above' | 'below')} className="input-surface mt-1 h-9 w-full rounded-lg border px-2 text-xs text-foreground"><option value="above">above</option><option value="below">below</option></select></label><label className="text-xs text-secondary-text">Target price<input value={targetPrice} onChange={(event) => setTargetPrice(event.target.value)} type="number" step="any" placeholder="optional" className="input-surface mt-1 h-9 w-full rounded-lg border px-2 text-xs text-foreground" /></label><label className="text-xs text-secondary-text">Invalidation price<input value={invalidationPrice} onChange={(event) => setInvalidationPrice(event.target.value)} type="number" step="any" placeholder="optional" className="input-surface mt-1 h-9 w-full rounded-lg border px-2 text-xs text-foreground" /></label><label className="text-xs text-secondary-text">Exit at<input value={exitAt} onChange={(event) => setExitAt(event.target.value)} type="datetime-local" className="input-surface mt-1 h-9 w-full rounded-lg border px-2 text-xs text-foreground" /></label></div>
      <p className="mt-3 text-xs text-secondary-text">{candidate.reasons.slice(0, 2).join(' · ')}</p>
    </Card>
  );
}

function AdviceVerdict({ job }: { job: TradeAdviceJob }) {
  const { t } = useUiLanguage();
  const [expanded, setExpanded] = useState(false);
  const text = textValue(job.explanation);
  const long = text.length > 420;
  const verdict = job.assessment === 'compare' ? t('tradeDesk.verdictCompare') : job.assessment === 'wait' ? t('tradeDesk.verdictWait') : textValue(job.assessment);
  return (
    <section className="mt-4 rounded-xl border border-cyan/25 bg-cyan/5 p-3" data-testid="advice-verdict">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-foreground">{t('tradeDesk.assessment')}</h3>
        {verdict ? <Badge variant={job.assessment === 'compare' ? 'info' : 'warning'}>{verdict}</Badge> : null}
      </div>
      <p className={`mt-2 whitespace-pre-line text-sm leading-6 text-secondary-text ${long && !expanded ? 'line-clamp-4' : ''}`}>{text}</p>
      {long ? <button type="button" className="mt-1 text-xs text-cyan hover:underline" onClick={() => setExpanded((value) => !value)}>{expanded ? t('tradeDesk.showLess') : t('tradeDesk.showMore')}</button> : null}
    </section>
  );
}

function ModelPanel({ panel }: { panel: TradeModelPanel }) {
  const { t } = useUiLanguage();
  const agreement = panel.agreement === 'agree' ? t('tradeDesk.panelAgree') : panel.agreement === 'split' ? t('tradeDesk.panelSplit') : t('tradeDesk.panelUnavailable');
  return (
    <section className="mt-4 rounded-xl border border-border/40 bg-card/30 p-3">
      <div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-semibold text-foreground">{t('tradeDesk.modelPanel')}</h3><Badge variant={panel.agreement === 'agree' ? 'success' : panel.agreement === 'split' ? 'warning' : 'default'}>{agreement}</Badge></div>
      <ul className="mt-2 space-y-2 text-xs text-secondary-text">
        {panel.opinions.map((opinion) => (
          <li key={opinion.backend}>
            <strong className="font-mono text-foreground">{opinion.model}</strong>{opinion.role === 'primary' ? ' (primary)' : ''}:{' '}
            {opinion.status !== 'ok' ? t('tradeDesk.panelUnavailable') : opinion.action === 'trade' ? `${t('tradeDesk.panelTrade')} ${opinion.strategy || ''}` : t('tradeDesk.panelWait')}
            {opinion.status === 'ok' && opinion.reason ? <span> — {opinion.reason}</span> : null}
            {opinion.status === 'ok' && opinion.risk ? <span className="block text-muted-text">Risk: {opinion.risk}</span> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

function AdviceJobCard({ job, selected, onSelect, onCancel, onFollowUp, isCancelling }: { job: TradeAdviceJob; selected: boolean; onSelect: () => void; onCancel: () => void; onFollowUp: (message: string) => void; isCancelling: boolean }) {
  const { t } = useUiLanguage();
  const [message, setMessage] = useState('');
  return <Card variant={selected ? 'gradient' : 'bordered'} padding="sm" className="cursor-pointer"><div className="flex items-start justify-between gap-3" onClick={onSelect}><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><strong className="font-mono text-foreground">{job.request.ticker}</strong><ModeBadge mode={job.request.dataMode} /><Badge variant={statusVariant(job.status)}>{statusLabel(job.status)}</Badge>{job.parentAdviceId ? <Badge variant="history">{t('tradeDesk.researchVersion')}</Badge> : null}</div><p className="mt-1 text-xs text-secondary-text">{formatDate(job.createdAt)} · {job.candidates.length} candidates</p></div>{job.status === 'queued' || job.status === 'running' ? <Button size="xsm" variant="ghost" isLoading={isCancelling} onClick={(event) => { event.stopPropagation(); onCancel(); }}><Pause className="h-3.5 w-3.5" />{t('tradeDesk.cancelJob')}</Button> : <ChevronRight className="mt-1 h-4 w-4 text-muted-text" />}</div>{selected && (job.assessment || job.explanation || job.error) ? <div className="mt-3 space-y-2 border-t border-border/40 pt-3 text-sm text-secondary-text"><p><strong className="text-foreground">{t('tradeDesk.assessment')}:</strong> {textValue(job.assessment)}</p><p><strong className="text-foreground">{t('tradeDesk.explanation')}:</strong> {textValue(job.explanation)}</p>{job.error ? <p className="text-danger"><strong>{t('tradeDesk.jobError')}:</strong> {job.error}</p> : null}<div className="flex flex-col gap-2 pt-2 sm:flex-row"><input aria-label={`${t('tradeDesk.followUp')} ${job.request.ticker}`} value={message} onChange={(event) => setMessage(event.target.value)} onClick={(event) => event.stopPropagation()} placeholder={t('tradeDesk.followUpPlaceholder')} className="input-surface h-9 min-w-0 flex-1 rounded-lg border px-3 text-xs text-foreground" /><Button size="sm" variant="outline" disabled={!message.trim()} onClick={(event) => { event.stopPropagation(); onFollowUp(message.trim()); setMessage(''); }}><Sparkles className="h-3.5 w-3.5" />{t('tradeDesk.followUp')}</Button></div></div> : null}</Card>;
}

function PositionCard({ position, plans, onPaperFill, onManualFill, onReconcile, onSettle, onUpdatePlan }: { position: TradePosition; plans: TradeDeskPlan[]; onPaperFill: (position: TradePosition) => void; onManualFill: (position: TradePosition) => void; onReconcile: (position: TradePosition) => void; onSettle: (position: TradePosition) => void; onUpdatePlan: (planId: string, changes: UpdateTradePlanRequest) => void }) {
  const { t } = useUiLanguage();
  const plan = plans.find((item) => item.id === position.planId) || position.plan;
  const replay = plan?.dataMode === 'replay';
  return <Card variant="bordered" padding="md"><div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex flex-wrap items-center gap-2"><h3 className="text-lg font-semibold text-foreground">{position.underlying}</h3><Badge variant={position.ledger === 'paper' ? 'info' : 'warning'}>{position.ledger === 'paper' ? t('tradeDesk.paper') : t('tradeDesk.manualLive')}</Badge>{plan ? <ModeBadge mode={plan.dataMode} /> : null}</div><p className="mt-1 text-xs text-secondary-text">Plan {position.planId} · {t('tradeDesk.positionStatus')}: {position.status}</p></div><div className="flex flex-wrap gap-2">{position.ledger === 'paper' ? <Button size="sm" variant="outline" onClick={() => onPaperFill(position)}><Play className="h-3.5 w-3.5" />{t('tradeDesk.paperFill')}</Button> : <Button size="sm" variant="outline" onClick={() => onManualFill(position)} disabled={replay}><CircleDollarSign className="h-3.5 w-3.5" />{t('tradeDesk.manualFill')}</Button>}{position.status === 'reconciliation_required' ? (position.ledger === 'paper'
      // Paper plans cannot record broker exercise/assignment; expired legs settle at intrinsic value.
      ? <Button size="sm" variant="secondary" onClick={() => onSettle(position)}><Check className="h-3.5 w-3.5" />Settle expiry</Button>
      : <Button size="sm" variant="secondary" onClick={() => onReconcile(position)}><Check className="h-3.5 w-3.5" />Reconcile</Button>) : null}{plan && position.status !== 'closed' ? <Button size="sm" variant="ghost" onClick={() => onUpdatePlan(plan.id, { monitoring: !plan.monitoring })}>{plan.monitoring ? 'Pause alerts' : 'Resume alerts'}</Button> : null}{plan && position.status === 'watching' && plan.status !== 'archived' ? <Button size="sm" variant="ghost" onClick={() => onUpdatePlan(plan.id, { status: 'archived', monitoring: false })}>Archive</Button> : null}</div></div><div className="mt-4 overflow-x-auto rounded-xl border border-border/50"><table className="w-full min-w-[600px] text-left text-xs"><thead className="bg-elevated/50 text-secondary-text"><tr><th className="px-3 py-2">{t('tradeDesk.contract')}</th><th className="px-3 py-2">Qty</th><th className="px-3 py-2">Average</th><th className="px-3 py-2">Mark</th><th className="px-3 py-2">P&L</th></tr></thead><tbody>{position.legs.map((leg) => <tr key={leg.contractId} className="border-t border-border/40"><td className="px-3 py-2 font-mono text-foreground">{leg.contractId}<span className="ml-2 text-secondary-text">{leg.right}</span></td><td className="px-3 py-2 text-foreground">{leg.signedQuantity || leg.quantity} × {leg.multiplier}</td><td className="px-3 py-2 text-foreground">{formatMoney(leg.averagePrice)}</td><td className="px-3 py-2 text-foreground">{formatMoney(leg.markPrice)}</td><td className="px-3 py-2 text-foreground">{formatMoney(leg.unrealizedPnl)}</td></tr>)}</tbody></table></div><div className="mt-3 grid gap-2 sm:grid-cols-4 text-sm"><div><span className="block text-xs text-secondary-text">{t('tradeDesk.realizedPnl')}</span><strong className="text-foreground">{formatMoney(position.realizedPnl)}</strong></div><div><span className="block text-xs text-secondary-text">{t('tradeDesk.unrealizedPnl')}</span><strong className="text-foreground">{formatMoney(position.unrealizedPnl)}</strong></div><div><span className="block text-xs text-secondary-text">{t('tradeDesk.fees')}</span><strong className="text-foreground">{formatMoney(position.fees)}</strong></div><div><span className="block text-xs text-secondary-text">{t('tradeDesk.valuation')}</span><strong className="text-foreground">{position.valuationStatus || t('tradeDesk.unknown')}</strong>{position.valuationAt ? <span className="ml-1 text-xs font-normal text-muted-text">({formatDate(position.valuationAt)})</span> : null}</div></div>{replay ? <p className="mt-3 text-xs text-warning">{t('tradeDesk.manualLiveReplayDisabled')}</p> : null}</Card>;
}

const TradeDeskPage: React.FC = () => {
  const { t } = useUiLanguage();
  const [searchParams, setSearchParams] = useSearchParams();
  const deepLinkTicker = searchParams.get('ticker')?.trim().toUpperCase() || '';
  const linkedAdviceId = searchParams.get('adviceId');
  const linkedPlanId = searchParams.get('planId');
  const focusedPlanId = useRef<string | null>(null);
  const sourceReportIdRaw = searchParams.get('sourceReportId');
  const sourceReportId = sourceReportIdRaw && /^\d+$/.test(sourceReportIdRaw) ? Number(sourceReportIdRaw) : undefined;
  const [view, setView] = useState<TradeDeskView>(linkedPlanId ? 'positions' : (searchParams.get('view') as TradeDeskView) || 'opportunities');
  const [health, setHealth] = useState<TradeDeskHealth | null>(null);
  const [catalog, setCatalog] = useState<TradeDeskCatalogItem[]>([]);
  const [advice, setAdvice] = useState<TradeAdviceJob[]>([]);
  const [selectedAdviceId, setSelectedAdviceId] = useState<string | null>(linkedAdviceId);
  const [plans, setPlans] = useState<TradeDeskPlan[]>([]);
  const [positions, setPositions] = useState<TradePosition[]>([]);
  const [journal, setJournal] = useState<TradeJournalEvent[]>([]);
  const [outcomes, setOutcomes] = useState<TradeOutcomes | null>(null);
  const [preferences, setPreferences] = useState<TradePreferences>(DEFAULT_PREFERENCES);
  const [form, setForm] = useState<AdviceFormState>(() => ({ ...DEFAULT_FORM, ticker: deepLinkTicker }));
  const [selectedStrategies, setSelectedStrategies] = useState<string[]>([]);
  const [followUpForm, setFollowUpForm] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [busyAdviceId, setBusyAdviceId] = useState<string | null>(null);
  const [busyPlanId, setBusyPlanId] = useState<string | null>(null);
  const [manualFillPosition, setManualFillPosition] = useState<TradePosition | null>(null);
  const [paperFillPosition, setPaperFillPosition] = useState<TradePosition | null>(null);
  const [paperFillIntent, setPaperFillIntent] = useState<'open' | 'close'>('open');
  const [paperFillQuantity, setPaperFillQuantity] = useState('1');
  const [paperFillLimit, setPaperFillLimit] = useState('');
  const [manualFill, setManualFill] = useState<ManualFillState>({ contractId: '', side: 'buy', quantity: '1', price: '', fees: '0', intent: 'open', note: '', filledAt: localDateTimeValue() });
  const [reconcilePosition, setReconcilePosition] = useState<TradePosition | null>(null);
  const [reconcileNotes, setReconcileNotes] = useState('');
  const [settlePosition, setSettlePosition] = useState<TradePosition | null>(null);
  const [settlePrice, setSettlePrice] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const lastEventId = useRef('');
  // Incremented by each list request and each locally created job so an
  // older list response cannot drop a newer job or move the selection.
  const adviceListSequence = useRef(0);
  const sseRefreshTimer = useRef<number | null>(null);

  useEffect(() => { document.title = 'Trade Desk - DSA'; }, []);
  // Apply a ?ticker= deep link once; clearing the field afterwards must not refill it.
  const appliedDeepLinkTicker = useRef('');
  useEffect(() => {
    if (!deepLinkTicker || appliedDeepLinkTicker.current === deepLinkTicker) return;
    appliedDeepLinkTicker.current = deepLinkTicker;
    setForm((current) => (current.ticker ? current : { ...current, ticker: deepLinkTicker }));
  }, [deepLinkTicker]);

  const refreshLedger = useCallback(async (includePreferences = false) => {
    // Preferences are an editable form; background refreshes must not revert unsaved edits.
    const results = await Promise.allSettled([tradeDeskApi.listPlans(), tradeDeskApi.listPositions(), tradeDeskApi.listJournal(), tradeDeskApi.getOutcomes(), ...(includePreferences ? [tradeDeskApi.getPreferences()] : [])]);
    if (results[0].status === 'fulfilled') setPlans(results[0].value.items || []);
    if (results[1].status === 'fulfilled') setPositions(results[1].value.items || []);
    if (results[2].status === 'fulfilled') setJournal(results[2].value.items || []);
    if (results[3].status === 'fulfilled') setOutcomes(results[3].value);
    if (results[4]?.status === 'fulfilled') setPreferences(results[4].value as TradePreferences);
    const rejected = results.find((result): result is PromiseRejectedResult => result.status === 'rejected');
    if (rejected) setError(errorMessage(rejected.reason));
  }, []);

  const refreshData = useCallback(async (showSpinner = true) => {
    if (showSpinner) { setIsLoading(true); setError(''); }
    const sequence = ++adviceListSequence.current;
    const results = await Promise.allSettled([tradeDeskApi.getHealth(), tradeDeskApi.getCatalog(), tradeDeskApi.listAdvice()]);
    if (results[0].status === 'fulfilled') setHealth(results[0].value);
    if (results[1].status === 'fulfilled') setCatalog(results[1].value.items || []);
    if (results[2].status === 'fulfilled' && sequence === adviceListSequence.current) {
      const items = [...(results[2].value.items || [])];
      if (linkedAdviceId && !items.some((item) => item.id === linkedAdviceId)) {
        try { items.unshift(await tradeDeskApi.getAdvice(linkedAdviceId)); }
        catch (linkError) { setError(errorMessage(linkError)); }
      }
      setAdvice(items);
      setSelectedAdviceId((current) => current && items.some((item) => item.id === current) ? current : linkedAdviceId || items[0]?.id || null);
    }
    const rejected = results.find((result): result is PromiseRejectedResult => result.status === 'rejected');
    if (rejected) setError(errorMessage(rejected.reason));
    await refreshLedger(showSpinner);
    if (showSpinner) setIsLoading(false);
  }, [refreshLedger, linkedAdviceId]);

  useEffect(() => { void refreshData(); }, [refreshData]);
  useEffect(() => { setSearchParams((current) => { const next = new URLSearchParams(current); next.set('view', view); return next; }, { replace: true }); }, [setSearchParams, view]);

  useEffect(() => {
    if (!health?.enabled || typeof EventSource === 'undefined') return undefined;
    const source = new EventSource(tradeDeskApi.getEventsUrl(lastEventId.current || undefined), { withCredentials: true });
    source.onmessage = (event) => {
      lastEventId.current = event.lastEventId || lastEventId.current;
      if (sseRefreshTimer.current == null) {
        sseRefreshTimer.current = window.setTimeout(() => {
          sseRefreshTimer.current = null;
          void refreshData(false);
        }, 500);
      }
    };
    source.onerror = () => { /* EventSource reconnects and sends Last-Event-ID. */ };
    return () => {
      source.close();
      if (sseRefreshTimer.current != null) {
        window.clearTimeout(sseRefreshTimer.current);
        sseRefreshTimer.current = null;
      }
    };
  }, [health?.enabled, refreshData]);

  useEffect(() => {
    if (linkedPlanId) setView('positions');
    else if (linkedAdviceId) { setSelectedAdviceId(linkedAdviceId); setView('opportunities'); }
  }, [linkedAdviceId, linkedPlanId]);

  useEffect(() => {
    if (view !== 'positions' || isLoading || !linkedPlanId || focusedPlanId.current === linkedPlanId) return;
    const target = document.getElementById(`trade-plan-${linkedPlanId}`);
    if (target) {
      target.scrollIntoView?.({ block: 'center' });
      target.focus({ preventScroll: true });
      focusedPlanId.current = linkedPlanId;
    }
  }, [view, isLoading, linkedPlanId, positions]);

  const activeAdviceIds = advice.filter((item) => item.status === 'queued' || item.status === 'running').map((item) => item.id).join(',');
  useEffect(() => {
    if (!activeAdviceIds) return undefined;
    const interval = window.setInterval(() => { void Promise.all(activeAdviceIds.split(',').map(async (id) => { try { const current = await tradeDeskApi.getAdvice(id); setAdvice((items) => items.map((item) => item.id === id ? current : item)); } catch { /* SSE and the next interval retry. */ } })); }, 2000);
    return () => window.clearInterval(interval);
  }, [activeAdviceIds]);

  useEffect(() => {
    if (view !== 'positions') return undefined;
    const interval = window.setInterval(() => {
      void tradeDeskApi.listPositions().then((result) => setPositions(result.items || [])).catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(interval);
  }, [view]);

  const selectedAdvice = useMemo(() => advice.find((item) => item.id === selectedAdviceId) || null, [advice, selectedAdviceId]);
  const intradayCandidates = selectedAdvice?.candidates.filter((candidate) => candidate.horizon === 'intraday') || [];
  const swingCandidates = selectedAdvice?.candidates.filter((candidate) => candidate.horizon === 'swing') || [];

  const adviceRequest = (): TradeAdviceRequest => ({
    ticker: form.ticker.trim().toUpperCase(),
    allocation: parseNumber(form.allocation),
    direction: form.direction,
    horizon: form.horizon,
    dataMode: form.dataMode,
    expiry: form.expiry || undefined,
    strategies: selectedStrategies,
    message: form.message.trim(),
    sourceReportId,
    existingShares: parseInteger(form.existingShares),
    feePerContract: parseNumber(form.feePerContract) ?? 0.65,
    riskFreeRate: parseNumber(form.riskFreeRate) ?? 0,
    dividendYield: parseNumber(form.dividendYield) ?? 0,
    marginPerUnit: parseNumber(form.marginPerUnit),
    planLegs: form.planLegs.trim() || undefined,
  });

  const submitAdvice = async () => {
    setError(''); setMessage('');
    if (!form.ticker.trim()) return;
    if (form.dataMode === 'live' && liveIsBlocked(health)) { setError(health?.live.message || t('tradeDesk.liveUnavailable')); return; }
    setIsSubmitting(true);
    try { const job = await tradeDeskApi.createAdvice(adviceRequest()); adviceListSequence.current += 1; setAdvice((items) => [job, ...items.filter((item) => item.id !== job.id)]); setSelectedAdviceId(job.id); setMessage(`Advice ${job.id} queued`); } catch (submitError) { setError(errorMessage(submitError)); } finally { setIsSubmitting(false); }
  };

  const cancelAdvice = async (job: TradeAdviceJob) => {
    setBusyAdviceId(job.id); setError('');
    try { const updated = await tradeDeskApi.cancelAdvice(job.id); setAdvice((items) => items.map((item) => item.id === job.id ? updated : item)); } catch (cancelError) { setError(errorMessage(cancelError)); } finally { setBusyAdviceId(null); }
  };

  const followUp = async (job: TradeAdviceJob, overrideMessage?: string) => {
    const content = (overrideMessage ?? followUpForm).trim();
    if (!content) return;
    setIsSubmitting(true); setError('');
    const baseline = { ...job.request };
    const effective = job.candidates.map((candidate) => job.effectiveRequests?.[candidate.id] || job.request);
    const fields: (keyof TradeAdviceRequest)[] = ['allocation', 'direction', 'horizon', 'expiry', 'strategies', 'existingShares', 'feePerContract', 'riskFreeRate', 'dividendYield', 'marginPerUnit'];
    for (const field of fields) {
      if (effective.length && effective.every((request) => JSON.stringify(request[field]) === JSON.stringify(effective[0][field]))) {
        Object.assign(baseline, { [field]: effective[0][field] });
      }
    }
    try { const child = await tradeDeskApi.createAdvice({ ...baseline, message: content, parentAdviceId: job.id }); adviceListSequence.current += 1; setAdvice((items) => [child, ...items]); setSelectedAdviceId(child.id); setFollowUpForm(''); } catch (followError) { setError(errorMessage(followError)); } finally { setIsSubmitting(false); }
  };

  const monitorCandidate = async (request: CreateTradePlanRequest) => {
    setBusyPlanId(request.candidateId); setError('');
    try { const plan = await tradeDeskApi.createPlan(request); setPlans((items) => [plan, ...items.filter((item) => item.id !== plan.id)]); setMessage(t('tradeDesk.monitoring')); } catch (monitorError) { setError(errorMessage(monitorError)); } finally { setBusyPlanId(null); }
  };

  const openPaperFill = (position: TradePosition) => { setPaperFillPosition(position); setPaperFillIntent('open'); setPaperFillQuantity('1'); setPaperFillLimit(''); };
  const submitPaperFill = async () => {
    if (!paperFillPosition) return;
    setBusyPlanId(paperFillPosition.planId); setError('');
    const quantity = parseQuantity(paperFillQuantity);
    if (quantity == null) { setError('Enter a whole-number quantity of at least 1.'); setBusyPlanId(null); return; }
    try { await tradeDeskApi.paperFill(paperFillPosition.planId, { intent: paperFillIntent, quantity, limitPrice: parseNumber(paperFillLimit) }); setPaperFillPosition(null); await refreshLedger(); setMessage(t('tradeDesk.paperFill')); } catch (fillError) { setError(errorMessage(fillError)); } finally { setBusyPlanId(null); }
  };

  // Every field is reset per plan so a previous dialog's intent, quantity or
  // note is never carried into another plan. Held legs default to closing.
  const manualFillDefaults = (position: TradePosition, contractId: string): Pick<ManualFillState, 'side' | 'intent' | 'price'> => {
    const plan = plans.find((item) => item.id === position.planId) || position.plan;
    const held = position.legs.find((leg) => leg.contractId === contractId);
    const opening = plan?.candidate.legs.find((leg) => leg.contractId === contractId);
    const mark = held?.markPrice;
    if (held && held.signedQuantity && position.status !== 'watching') {
      return { side: held.signedQuantity > 0 ? 'sell' : 'buy', intent: 'close', price: mark == null ? '' : String(mark) };
    }
    return { side: opening?.side === 'sell' ? 'sell' : 'buy', intent: 'open', price: mark == null ? '' : String(mark) };
  };
  const openManualFill = (position: TradePosition) => {
    const plan = plans.find((item) => item.id === position.planId) || position.plan;
    const heldLeg = position.status !== 'watching' ? position.legs.find((leg) => !(leg.right === 'stock' && plan?.candidate.legs.some((item) => item.existing && item.contractId === leg.contractId))) : undefined;
    const contractId = heldLeg?.contractId || plan?.candidate.legs.find((leg) => !leg.existing)?.contractId || plan?.candidate.underlying || '';
    setManualFillPosition(position);
    setManualFill({ contractId, quantity: '1', fees: '0', note: '', filledAt: localDateTimeValue(), ...manualFillDefaults(position, contractId) });
  };
  const submitManualFill = async () => {
    if (!manualFillPosition || !manualFill.contractId || parseNumber(manualFill.price) == null) return;
    const quantity = parseQuantity(manualFill.quantity);
    if (quantity == null) { setError('Enter a whole-number quantity of at least 1.'); return; }
    if (!manualFill.filledAt || Number.isNaN(new Date(manualFill.filledAt).getTime())) { setError('Enter the actual fill time.'); return; }
    setBusyPlanId(manualFillPosition.planId); setError('');
    try { await tradeDeskApi.createFill(manualFillPosition.planId, { contractId: manualFill.contractId, side: manualFill.side, quantity, price: parseNumber(manualFill.price) || 0, fees: parseNumber(manualFill.fees) || 0, filledAt: new Date(manualFill.filledAt).toISOString(), intent: manualFill.intent, note: manualFill.note }); setManualFillPosition(null); await refreshLedger(); } catch (fillError) { setError(errorMessage(fillError)); } finally { setBusyPlanId(null); }
  };
  const submitReconcile = async () => {
    if (!reconcilePosition || !reconcileNotes.trim()) return;
    setBusyPlanId(reconcilePosition.planId); setError('');
    try { await tradeDeskApi.reconcilePlan(reconcilePosition.planId, { notes: reconcileNotes.trim() }); setReconcilePosition(null); setReconcileNotes(''); await refreshLedger(); } catch (reconcileError) { setError(errorMessage(reconcileError)); } finally { setBusyPlanId(null); }
  };
  const submitSettle = async () => {
    const price = parseNumber(settlePrice);
    if (!settlePosition || price == null || price <= 0) { setError('Enter the underlying price at expiration.'); return; }
    setBusyPlanId(settlePosition.planId); setError('');
    try { await tradeDeskApi.paperSettle(settlePosition.planId, price); setSettlePosition(null); setSettlePrice(''); await refreshLedger(); } catch (settleError) { setError(errorMessage(settleError)); } finally { setBusyPlanId(null); }
  };
  const updatePlan = async (planId: string, changes: UpdateTradePlanRequest) => {
    setBusyPlanId(planId); setError('');
    try { const plan = await tradeDeskApi.updatePlan(planId, changes); setPlans((items) => items.map((item) => item.id === plan.id ? plan : item)); await refreshLedger(); } catch (planError) { setError(errorMessage(planError)); } finally { setBusyPlanId(null); }
  };
  const savePreferences = async () => {
    setError('');
    try { setPreferences(await tradeDeskApi.updatePreferences(preferences)); setMessage(t('tradeDesk.savePreferences')); } catch (preferenceError) { setError(errorMessage(preferenceError)); }
  };

  const setActiveView = (nextView: TradeDeskView) => { setView(nextView); };
  const renderJobList = () => <div className="space-y-3">{advice.length ? advice.slice(0, 20).map((job) => <AdviceJobCard key={job.id} job={job} selected={job.id === selectedAdviceId} onSelect={() => setSelectedAdviceId(job.id)} onCancel={() => void cancelAdvice(job)} onFollowUp={(content) => void followUp(job, content)} isCancelling={busyAdviceId === job.id} />) : <EmptyState icon={<FileQuestion className="h-8 w-8" />} title={t('tradeDesk.noCandidates')} description={t('tradeDesk.description')} />}</div>;
  const renderOpportunities = () => <div className="space-y-5"><AdviceForm form={form} setForm={setForm} catalog={catalog} health={health} selectedStrategies={selectedStrategies} setSelectedStrategies={setSelectedStrategies} onSubmit={() => void submitAdvice()} isSubmitting={isSubmitting} sourceReportId={sourceReportId} />{selectedAdvice ? <Card variant="bordered" padding="md"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="flex flex-wrap items-center gap-2"><h2 className="text-lg font-semibold text-foreground">{selectedAdvice.request.ticker} opportunities</h2><ModeBadge mode={selectedAdvice.request.dataMode} /><Badge variant={statusVariant(selectedAdvice.status)}>{statusLabel(selectedAdvice.status)}</Badge></div><p className="mt-1 text-xs text-secondary-text">{formatDate(selectedAdvice.updatedAt)} · snapshot {textValue(selectedAdvice.snapshot && (selectedAdvice.snapshot as { id?: unknown }).id) || 'pending'}</p></div><Button size="sm" variant="ghost" onClick={() => void refreshData(false)}><RefreshCw className="h-3.5 w-3.5" />{t('tradeDesk.refresh')}</Button></div>{selectedAdvice.request.dataMode === 'replay' ? <InlineAlert className="mt-4" variant="info" message={t('tradeDesk.replaySynthetic')} /> : null}{selectedAdvice.planError ? <InlineAlert className="mt-3" variant="warning" title={t('tradeDesk.planNotPriced')} message={selectedAdvice.planError} /> : null}{selectedAdvice.panel?.opinions?.length ? <ModelPanel panel={selectedAdvice.panel} /> : null}{selectedAdvice.explanation ? <AdviceVerdict job={selectedAdvice} /> : null}{selectedAdvice.status === 'stale' ? <InlineAlert className="mt-3" variant="warning" message={t('tradeDesk.staleAdvice')} action={<Button size="sm" variant="outline" isLoading={isSubmitting} onClick={() => void followUp(selectedAdvice, selectedAdvice.request.message || t('tradeDesk.runAgain'))}>{t('tradeDesk.runAgain')}</Button>} /> : null}<div className="mt-5 space-y-5">{intradayCandidates.length ? <section><h3 className="mb-3 text-sm font-semibold text-foreground">{t('tradeDesk.intraday')}</h3><div className="space-y-4">{intradayCandidates.map((candidate) => <CandidateCard key={candidate.id} candidate={candidate} advice={selectedAdvice} onMonitor={monitorCandidate} monitoring={busyPlanId === candidate.id || plans.some((plan) => plan.adviceId === selectedAdvice.id && (plan.candidateId === candidate.id || plan.candidate.id === candidate.id))} />)}</div></section> : null}{swingCandidates.length ? <section><h3 className="mb-3 text-sm font-semibold text-foreground">{t('tradeDesk.swing')}</h3><div className="space-y-4">{swingCandidates.map((candidate) => <CandidateCard key={candidate.id} candidate={candidate} advice={selectedAdvice} onMonitor={monitorCandidate} monitoring={busyPlanId === candidate.id || plans.some((plan) => plan.adviceId === selectedAdvice.id && (plan.candidateId === candidate.id || plan.candidate.id === candidate.id))} />)}</div></section> : null}{selectedAdvice.status === 'completed' && selectedAdvice.candidates.length === 0 ? <EmptyState title={t('tradeDesk.noCandidates')} description={textValue(selectedAdvice.explanation)} /> : null}</div></Card> : null}<div className="grid gap-5 lg:grid-cols-[minmax(0,1.1fr)_minmax(300px,0.9fr)]"><div><div className="mb-3 flex items-center justify-between"><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.researchVersion')}</h2><span className="text-xs text-secondary-text">{advice.length}</span></div>{renderJobList()}</div><Card variant="bordered" padding="md"><div className="flex items-center gap-2"><LineChartIcon className="h-5 w-5 text-cyan" /><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.followUp')}</h2></div><p className="mt-2 text-sm leading-6 text-secondary-text">{t('tradeDesk.followUpPlaceholder')}</p><textarea value={followUpForm} onChange={(event) => setFollowUpForm(event.target.value)} rows={5} disabled={!selectedAdvice} placeholder={selectedAdvice ? t('tradeDesk.followUpPlaceholder') : t('tradeDesk.noCandidates')} className="input-surface mt-4 w-full rounded-xl border px-3 py-2 text-sm text-foreground" /><Button className="mt-3 w-full" variant="outline" disabled={!selectedAdvice || !followUpForm.trim()} isLoading={isSubmitting} onClick={() => selectedAdvice && void followUp(selectedAdvice)}><Sparkles className="h-4 w-4" />{t('tradeDesk.followUp')}</Button></Card></div></div>;
  const renderAsk = () => <div className="space-y-5"><AdviceForm form={form} setForm={setForm} catalog={catalog} health={health} selectedStrategies={selectedStrategies} setSelectedStrategies={setSelectedStrategies} onSubmit={() => void submitAdvice()} isSubmitting={isSubmitting} sourceReportId={sourceReportId} /><InlineAlert variant="info" title={t('tradeDesk.explanation')} message="The answer is stored as a versioned advice job. Return to Opportunities to compare candidates, monitor a plan, or ask another question." /></div>;
  const manualFillContracts = manualFillPosition ? (() => {
    const plan = plans.find((item) => item.id === manualFillPosition.planId) || manualFillPosition.plan;
    const candidateContracts = plan?.candidate.legs.map((leg) => leg.contractId) || [];
    const heldContracts = manualFillPosition.legs.map((leg) => leg.contractId);
    return Array.from(new Set([...heldContracts, ...candidateContracts, plan?.candidate.underlying].filter((item): item is string => Boolean(item))));
  })() : [];
  const renderPositions = () => <div className="space-y-5"><InlineAlert variant="info" message={t('tradeDesk.noLiveOrders')} />{paperFillPosition ? <Card variant="gradient" padding="md"><div className="flex items-center justify-between"><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.paperFill')} · {paperFillPosition.underlying}</h2><Button size="sm" variant="ghost" onClick={() => setPaperFillPosition(null)}><X className="h-4 w-4" />{t('tradeDesk.close')}</Button></div><div className="mt-4 grid gap-3 md:grid-cols-3"><label className="text-xs text-secondary-text">{t('tradeDesk.intent')}<select value={paperFillIntent} onChange={(event) => setPaperFillIntent(event.target.value as 'open' | 'close')} className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground"><option value="open">{t('tradeDesk.open')}</option><option value="close">{t('tradeDesk.closeIntent')}</option></select></label><label className="text-xs text-secondary-text">{t('tradeDesk.quantity')}<input value={paperFillQuantity} onChange={(event) => setPaperFillQuantity(event.target.value)} type="number" min="1" step="1" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label><label className="text-xs text-secondary-text">Net debit limit (whole fill, USD; negative = minimum credit)<input value={paperFillLimit} onChange={(event) => setPaperFillLimit(event.target.value)} type="number" step="any" placeholder="optional" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label></div><Button className="mt-3" onClick={() => void submitPaperFill()} isLoading={busyPlanId === paperFillPosition.planId} disabled={!paperFillQuantity.trim()}><Play className="h-4 w-4" />{t('tradeDesk.paperFill')}</Button></Card> : null}{plans.length ? <Card variant="bordered" padding="md"><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.monitoring')}</h2><div className="mt-3 grid gap-2 md:grid-cols-2">{plans.map((plan) => <div key={plan.id} className="rounded-xl border border-border/50 p-3 text-sm"><div className="flex items-center justify-between gap-2"><span className="font-mono text-foreground">{plan.candidate.underlying}</span><div className="flex gap-1"><ModeBadge mode={plan.dataMode} /><Badge variant={plan.ledger === 'paper' ? 'info' : 'warning'}>{plan.ledger}</Badge></div></div><p className="mt-1 text-xs text-secondary-text">{plan.candidate.title} · {plan.status} · {plan.monitoring ? t('tradeDesk.monitoring') : 'paused'}</p></div>)}</div></Card> : null}{positions.length ? <div className="space-y-4">{positions.map((position) => <div key={position.planId} id={`trade-plan-${position.planId}`} tabIndex={-1}><PositionCard position={position} plans={plans} onPaperFill={openPaperFill} onManualFill={openManualFill} onUpdatePlan={(planId, changes) => void updatePlan(planId, changes)} onSettle={(item) => { setSettlePosition(item); setSettlePrice(''); }} onReconcile={(item) => setReconcilePosition(item)} /></div>)}</div> : <EmptyState icon={<ShieldCheck className="h-8 w-8" />} title={t('tradeDesk.noPositions')} description={t('tradeDesk.noLiveOrders')} />}{manualFillPosition ? <Card variant="gradient" padding="md"><div className="flex items-center justify-between"><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.manualFill')} · {manualFillPosition.underlying}</h2><Button size="sm" variant="ghost" onClick={() => setManualFillPosition(null)}><X className="h-4 w-4" />{t('tradeDesk.close')}</Button></div><div className="mt-4 grid gap-3 md:grid-cols-3"><label className="text-xs text-secondary-text">{t('tradeDesk.contract')}<select value={manualFill.contractId} onChange={(event) => { const contractId = event.target.value; setManualFill((current) => ({ ...current, contractId, ...(manualFillPosition ? manualFillDefaults(manualFillPosition, contractId) : {}) })); }} className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground">{manualFillContracts.map((contractId) => <option key={contractId} value={contractId}>{contractId}</option>)}</select></label><label className="text-xs text-secondary-text">{t('tradeDesk.side')}<select value={manualFill.side} onChange={(event) => setManualFill((current) => ({ ...current, side: event.target.value as 'buy' | 'sell' }))} className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground"><option value="buy">buy</option><option value="sell">sell</option></select></label><label className="text-xs text-secondary-text">{t('tradeDesk.quantity')}<input value={manualFill.quantity} onChange={(event) => setManualFill((current) => ({ ...current, quantity: event.target.value }))} type="number" min="1" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label><label className="text-xs text-secondary-text">{t('tradeDesk.price')}<input value={manualFill.price} onChange={(event) => setManualFill((current) => ({ ...current, price: event.target.value }))} type="number" step="any" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label><label className="text-xs text-secondary-text">{t('tradeDesk.fees')}<input value={manualFill.fees} onChange={(event) => setManualFill((current) => ({ ...current, fees: event.target.value }))} type="number" min="0" step="any" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label><label className="text-xs text-secondary-text">{t('tradeDesk.intent')}<select value={manualFill.intent} onChange={(event) => setManualFill((current) => ({ ...current, intent: event.target.value as TradeFillIntent }))} className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground"><option value="open">{t('tradeDesk.open')}</option><option value="close">{t('tradeDesk.closeIntent')}</option><option value="assignment">assignment</option><option value="exercise">exercise</option></select></label><label className="text-xs text-secondary-text">Filled at<input aria-label="Filled at" type="datetime-local" value={manualFill.filledAt} onChange={(event) => setManualFill((current) => ({ ...current, filledAt: event.target.value }))} className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label></div><label className="mt-3 block text-xs text-secondary-text">{t('tradeDesk.note')}<textarea value={manualFill.note} onChange={(event) => setManualFill((current) => ({ ...current, note: event.target.value }))} rows={2} className="input-surface mt-1 w-full rounded-lg border px-3 py-2 text-sm text-foreground" /></label><Button className="mt-3" onClick={() => void submitManualFill()} isLoading={busyPlanId === manualFillPosition.planId} disabled={!manualFill.contractId || parseNumber(manualFill.price) == null}><CircleDollarSign className="h-4 w-4" />{t('tradeDesk.submitFill')}</Button></Card> : null}{settlePosition ? <Card variant="gradient" padding="md"><div className="flex items-center justify-between"><h2 className="text-lg font-semibold text-foreground">Settle expiry · {settlePosition.underlying}</h2><Button size="sm" variant="ghost" onClick={() => setSettlePosition(null)}><X className="h-4 w-4" />{t('tradeDesk.close')}</Button></div><p className="mt-2 text-xs text-secondary-text">Expired paper option legs close at intrinsic value for this underlying price. Share delivery is not simulated.</p><label className="mt-3 block text-xs text-secondary-text">Underlying price at expiration (USD)<input aria-label="Underlying price at expiration" value={settlePrice} onChange={(event) => setSettlePrice(event.target.value)} type="number" min="0" step="any" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label><Button className="mt-3" onClick={() => void submitSettle()} isLoading={busyPlanId === settlePosition.planId} disabled={!(parseNumber(settlePrice) && (parseNumber(settlePrice) ?? 0) > 0)}><Check className="h-4 w-4" />Settle expiry</Button></Card> : null}{reconcilePosition ? <Card variant="gradient" padding="md"><div className="flex items-center justify-between"><h2 className="text-lg font-semibold text-foreground">Reconcile {reconcilePosition.underlying}</h2><Button size="sm" variant="ghost" onClick={() => setReconcilePosition(null)}><X className="h-4 w-4" />{t('tradeDesk.close')}</Button></div><textarea value={reconcileNotes} onChange={(event) => setReconcileNotes(event.target.value)} rows={3} placeholder="Record broker assignment/exercise details" className="input-surface mt-3 w-full rounded-xl border px-3 py-2 text-sm text-foreground" /><Button className="mt-3" onClick={() => void submitReconcile()} isLoading={busyPlanId === reconcilePosition.planId} disabled={!reconcileNotes.trim()}><Check className="h-4 w-4" />Reconcile</Button></Card> : null}</div>;
  // Replay events must never read as live: resolve each event's data mode from
  // its payload, plan or advice so the journal can label it.
  const journalMode = (event: TradeJournalEvent): string | undefined => {
    const payload = event.payload || {};
    if (typeof payload.dataMode === 'string') return payload.dataMode;
    const planId = event.planId || (typeof payload.planId === 'string' ? payload.planId : undefined);
    const plan = planId ? plans.find((item) => item.id === planId) : undefined;
    if (plan) return plan.dataMode;
    const adviceId = event.adviceId || (typeof payload.adviceId === 'string' ? payload.adviceId : undefined);
    return adviceId ? advice.find((item) => item.id === adviceId)?.request.dataMode : undefined;
  };
  const renderJournal = () => <div className="space-y-5"><div className="grid gap-4 md:grid-cols-2"><Card variant="bordered" padding="md"><div className="flex items-center gap-2"><BookOpen className="h-5 w-5 text-cyan" /><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.outcomes')}</h2></div><div className="mt-4 grid gap-4 sm:grid-cols-2">{(['paper', 'paperReplay', 'manualLive'] as const).map((ledger) => { const bucket = outcomes?.[ledger]; return <div key={ledger} className="rounded-xl bg-elevated/50 p-3"><div className="flex items-center justify-between gap-2"><span className="flex flex-wrap items-center gap-2 text-sm font-semibold text-foreground">{ledger === 'manualLive' ? t('tradeDesk.manualLive') : t('tradeDesk.paper')}{ledger === 'paperReplay' ? <ModeBadge mode="replay" /> : null}</span><Badge variant={ledger === 'paper' ? 'info' : 'warning'}>{bucket?.closedTrades ?? 0}</Badge></div><p className="mt-2 text-sm text-secondary-text">{t('tradeDesk.realizedPnl')}: <strong className="text-foreground">{formatMoney(bucket?.realizedPnl)}</strong></p><p className="mt-1 text-xs text-secondary-text">{t('tradeDesk.winRate')}: {formatPercent(bucket?.winRate)}</p>{typeof bucket?.note === 'string' ? <p className="mt-1 text-xs text-muted-text">{bucket.note}</p> : null}</div>; })}</div></Card><Card variant="bordered" padding="md"><div className="flex items-center gap-2"><Settings2 className="h-5 w-5 text-cyan" /><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.preferences')}</h2></div><div className="mt-4 space-y-3 text-sm"><label className="flex items-center justify-between gap-3"><span>{t('tradeDesk.proactive')}</span><input type="checkbox" checked={preferences.proactiveEnabled} onChange={(event) => setPreferences((current) => ({ ...current, proactiveEnabled: event.target.checked }))} /></label><label className="flex items-center justify-between gap-3"><span>{t('tradeDesk.discord')}</span><input type="checkbox" checked={preferences.discordEnabled} onChange={(event) => setPreferences((current) => ({ ...current, discordEnabled: event.target.checked }))} /></label><div className="grid grid-cols-2 gap-3"><label className="text-xs text-secondary-text">{t('tradeDesk.dailyLimit')}<input type="number" min="1" max="50" value={preferences.opportunityDailyLimit} onChange={(event) => setPreferences((current) => ({ ...current, opportunityDailyLimit: parseInteger(event.target.value, 3) }))} className="input-surface mt-1 h-9 w-full rounded-lg border px-2 text-sm text-foreground" /></label><label className="text-xs text-secondary-text">{t('tradeDesk.cooldown')}<input type="number" min="1" max="1440" value={preferences.cooldownMinutes} onChange={(event) => setPreferences((current) => ({ ...current, cooldownMinutes: parseInteger(event.target.value, 60) }))} className="input-surface mt-1 h-9 w-full rounded-lg border px-2 text-sm text-foreground" /></label></div><div className="flex flex-wrap items-center justify-between gap-2"><Button size="sm" onClick={() => void savePreferences()}>{t('tradeDesk.savePreferences')}</Button><Link className="text-xs text-cyan hover:underline" to="/settings">{t('tradeDesk.discordSettings')}</Link></div></div></Card></div>{journal.length ? <Card variant="bordered" padding="md"><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.journal')}</h2><div className="mt-3 divide-y divide-border/40">{journal.map((event) => <div key={String(event.id)} className="grid gap-2 py-3 text-sm md:grid-cols-[150px_1fr_180px]"><div className="flex flex-wrap items-start gap-2 font-medium text-foreground">{event.eventType}{journalMode(event) ? <ModeBadge mode={journalMode(event) as TradeDeskDataMode} /> : null}</div><div className="text-secondary-text">{Object.entries(event.payload || {}).map(([key, value]) => <span key={key} className="mr-3 inline-block"><span className="text-muted-text">{key}</span>: {textValue(value)}</span>)}</div><div className="text-xs text-muted-text">{formatDate(event.createdAt)}</div></div>)}</div></Card> : <EmptyState icon={<BookOpen className="h-8 w-8" />} title={t('tradeDesk.noJournal')} description={t('tradeDesk.description')} />}</div>;

  if (isLoading && !health) return <AppPage><Loading label={t('common.loading')} /></AppPage>;
  if (health && !health.enabled) return <AppPage><InlineAlert variant="warning" title={t('tradeDesk.unavailable')} message="Trade Desk is disabled by the server configuration." /></AppPage>;
  return <AppPage><PageHeader eyebrow={t('tradeDesk.eyebrow')} title={t('tradeDesk.title')} description={t('tradeDesk.description')} actions={<><Button size="sm" variant="ghost" onClick={() => void refreshData()}><RefreshCw className="h-4 w-4" />{t('tradeDesk.refresh')}</Button><Link to="/settings" className="inline-flex h-9 items-center gap-2 rounded-lg border border-border/60 px-3 text-sm text-secondary-text hover:text-foreground"><Settings2 className="h-4 w-4" />Settings</Link></>} /><div className="mt-4 flex flex-wrap gap-2 rounded-2xl border border-border/50 bg-card/50 p-2" role="tablist" aria-label={t('tradeDesk.title')}>{([['opportunities', t('tradeDesk.opportunities')], ['ask', t('tradeDesk.ask')], ['positions', t('tradeDesk.positions')], ['journal', t('tradeDesk.journal')]] as const).map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={view === key} onClick={() => setActiveView(key)} className={`rounded-xl px-4 py-2 text-sm transition ${view === key ? 'bg-cyan/10 text-cyan' : 'text-secondary-text hover:text-foreground'}`}>{label}</button>)}</div>{error ? <InlineAlert className="mt-4" variant="danger" title={t('common.failure')} message={error} action={<Button size="sm" variant="ghost" onClick={() => setError('')}>{t('common.close')}</Button>} /> : null}{message ? <InlineAlert className="mt-4" variant="success" message={message} /> : null}<div className="mt-5">{view === 'opportunities' ? renderOpportunities() : view === 'ask' ? renderAsk() : view === 'positions' ? renderPositions() : renderJournal()}</div></AppPage>;
};

export default TradeDeskPage;
