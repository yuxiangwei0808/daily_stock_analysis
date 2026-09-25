import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Bell, BellOff, Pause, Play, Plus, RefreshCw, Sparkles, Trash2, Wallet, X } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Badge, Button, Card, EmptyState, InlineAlert, Loading } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiTextKey } from '../../i18n/uiText';
import type {
  HoldingOption,
  HoldingRule,
  HoldingRuleKind,
  HoldingStock,
  HoldingsResponse,
  PortfolioSummary,
} from '../../types/tradeDesk';

const POLL_MS = 30_000;
const inputClass = 'input-surface mt-1 h-9 w-full rounded-lg border px-2 text-sm text-foreground';

function money(value?: number | null, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '-';
  const sign = value < 0 ? '-' : '';
  return `${sign}$${Math.abs(value).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function pct(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return '-';
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`;
}

function tone(value?: number | null): string {
  if (value == null || value === 0) return 'text-foreground';
  return value > 0 ? 'text-success' : 'text-danger';
}

function errorText(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail) return String((detail as { message: unknown }).message);
  return error instanceof Error ? error.message : String(error);
}

type RuleTarget = { positionKey: string | null; ticker: string; isOption: boolean; label: string };

function ruleText(rule: HoldingRule, t: (key: UiTextKey) => string, withTarget = false): string {
  const unit = rule.kind.startsWith('pnl') ? '%' : '';
  const text = `${t(`tradeDesk.holdings.kind.${rule.kind}` as UiTextKey)} ${rule.value}${unit}`;
  return withTarget ? `${rule.positionLabel || rule.ticker} · ${text}` : text;
}

function SummaryLine({ text }: { text: string }) {
  // Discord markdown subset: **bold** and _italic_ lines.
  const parts = text.replace(/^_(.*)_$/, '$1').split(/(\*\*[^*]+\*\*)/g);
  return (
    <p className={text.startsWith('_') ? 'text-xs text-muted-text' : text.startsWith('**') ? 'mt-2 font-semibold text-foreground' : 'text-sm text-secondary-text'}>
      {parts.map((part, index) => (part.startsWith('**') ? <strong key={index} className="text-foreground">{part.slice(2, -2)}</strong> : part))}
    </p>
  );
}

function SummaryCard({ summary, onBuilt }: { summary?: PortfolioSummary | null; onBuilt: (summary: PortfolioSummary) => void }) {
  const { t } = useUiLanguage();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');
  const build = async () => {
    setBusy(true);
    setProblem('');
    try {
      onBuilt(await tradeDeskApi.buildPortfolioSummary());
    } catch (error) {
      setProblem(errorText(error));
    } finally {
      setBusy(false);
    }
  };
  const lines = (summary?.message ?? '').split('\n').filter((line) => line.trim());
  return (
    <Card variant="bordered" padding="md">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-secondary-text">{t('tradeDesk.holdings.summary')}</h2>
          <p className="mt-1 text-xs text-muted-text">
            {summary?.builtAt ? `${t('tradeDesk.holdings.summaryBuilt')} ${new Date(summary.builtAt).toLocaleString()}` : t('tradeDesk.holdings.summaryNone')}
          </p>
        </div>
        <Button size="sm" variant="ghost" onClick={() => void build()} isLoading={busy}><RefreshCw className="h-4 w-4" />{t('tradeDesk.holdings.summaryBuild')}</Button>
      </div>
      {problem ? <p className="mt-2 text-xs text-danger">{problem}</p> : null}
      {lines.length ? <div className="mt-3 space-y-1">{lines.slice(1).map((line, index) => <SummaryLine key={index} text={line} />)}</div> : null}
    </Card>
  );
}

function RuleForm({ target, onSaved, onCancel }: { target: RuleTarget; onSaved: (warning?: string) => void; onCancel: () => void }) {
  const { t } = useUiLanguage();
  const kinds = useMemo<HoldingRuleKind[]>(() => (target.isOption
    ? ['price_below', 'price_above', 'days_to_expiry', 'pnl_below', 'pnl_above']
    : target.positionKey ? ['price_below', 'price_above', 'pnl_below', 'pnl_above'] : ['price_below', 'price_above']), [target]);
  const [text, setText] = useState('');
  const [kind, setKind] = useState<HoldingRuleKind>('price_below');
  const [value, setValue] = useState('');
  const [repeat, setRepeat] = useState<'once' | 'daily'>('once');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState<'parse' | 'save' | null>(null);
  const [problem, setProblem] = useState('');

  const readText = async () => {
    setBusy('parse');
    setProblem('');
    try {
      const draft = await tradeDeskApi.parseHoldingRule(text, target.positionKey);
      if (!kinds.includes(draft.kind)) throw new Error(t('tradeDesk.holdings.kindNotAllowed'));
      setKind(draft.kind);
      setValue(String(draft.value));
    } catch (error) {
      setProblem(errorText(error));
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    const number = Number(value);
    if (!value.trim() || !Number.isFinite(number)) {
      setProblem(t('tradeDesk.holdings.valueRequired'));
      return;
    }
    setBusy('save');
    setProblem('');
    try {
      const created = await tradeDeskApi.createHoldingRule({ positionKey: target.positionKey, ticker: target.ticker, kind, value: number,
        note: note.trim(), repeat });
      onSaved(created.warning);
    } catch (error) {
      setProblem(errorText(error));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mt-3 rounded-xl border border-cyan/30 bg-cyan/5 p-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium text-foreground">{t('tradeDesk.holdings.newAlert')} · {target.label}</p>
        <Button size="xsm" variant="ghost" onClick={onCancel} aria-label={t('tradeDesk.close')}><X className="h-4 w-4" /></Button>
      </div>
      <label className="mt-2 block text-xs text-secondary-text">
        {t('tradeDesk.holdings.describe')}
        <div className="mt-1 flex gap-2">
          <input value={text} onChange={(event) => setText(event.target.value)} placeholder={t('tradeDesk.holdings.describePlaceholder')}
            className="input-surface h-9 w-full rounded-lg border px-2 text-sm text-foreground"
            onKeyDown={(event) => { if (event.key === 'Enter' && text.trim()) void readText(); }} />
          <Button size="sm" variant="secondary" onClick={() => void readText()} disabled={!text.trim()} isLoading={busy === 'parse'}>
            <Sparkles className="h-4 w-4" />{t('tradeDesk.holdings.read')}
          </Button>
        </div>
      </label>
      <div className="mt-3 grid gap-3 sm:grid-cols-4">
        <label className="text-xs text-secondary-text sm:col-span-2">
          {t('tradeDesk.holdings.when')}
          <select value={kind} onChange={(event) => setKind(event.target.value as HoldingRuleKind)} className={inputClass}>
            {kinds.map((item) => <option key={item} value={item}>{t(`tradeDesk.holdings.kind.${item}` as UiTextKey)}</option>)}
          </select>
        </label>
        <label className="text-xs text-secondary-text">
          {kind === 'days_to_expiry' ? t('tradeDesk.holdings.days') : kind.startsWith('pnl') ? t('tradeDesk.holdings.percent') : t('tradeDesk.holdings.price')}
          <input value={value} onChange={(event) => setValue(event.target.value)} type="number" step="any" className={inputClass} />
        </label>
        <label className="text-xs text-secondary-text">
          {t('tradeDesk.holdings.repeat')}
          <select value={repeat} onChange={(event) => setRepeat(event.target.value as 'once' | 'daily')} className={inputClass}>
            <option value="once">{t('tradeDesk.holdings.once')}</option>
            <option value="daily">{t('tradeDesk.holdings.daily')}</option>
          </select>
        </label>
      </div>
      <label className="mt-3 block text-xs text-secondary-text">
        {t('tradeDesk.note')}
        <input value={note} onChange={(event) => setNote(event.target.value)} maxLength={200} placeholder={t('tradeDesk.holdings.notePlaceholder')} className={inputClass} />
      </label>
      {problem ? <p className="mt-2 text-xs text-danger">{problem}</p> : null}
      <Button className="mt-3" size="sm" onClick={() => void save()} isLoading={busy === 'save'}><Bell className="h-4 w-4" />{t('tradeDesk.holdings.saveAlert')}</Button>
    </div>
  );
}

function RuleList({ rules, onChanged, withTarget = false }: { rules: HoldingRule[]; onChanged: () => void; withTarget?: boolean }) {
  const { t } = useUiLanguage();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [problem, setProblem] = useState('');
  if (!rules.length) return null;
  const run = async (rule: HoldingRule, action: () => Promise<unknown>) => {
    setBusyId(rule.id);
    setProblem('');
    try {
      await action();
      onChanged();
    } catch (error) {
      setProblem(errorText(error));
      onChanged();
    } finally {
      setBusyId(null);
    }
  };
  return (
    <ul className="mt-3 space-y-2">
      {problem ? <li className="text-xs text-danger">{problem}</li> : null}
      {rules.map((rule) => (
        <li key={rule.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-elevated/50 px-3 py-2 text-sm">
          <div className="min-w-0">
            <span className="font-medium text-foreground">{ruleText(rule, t, withTarget)}</span>
            <span className="ml-2 text-xs text-muted-text">{rule.repeat === 'daily' ? t('tradeDesk.holdings.daily') : t('tradeDesk.holdings.once')}</span>
            {rule.note ? <p className="truncate text-xs text-secondary-text">{rule.note}</p> : null}
          </div>
          <div className="flex items-center gap-1">
            <Badge variant={rule.status === 'active' ? 'success' : rule.status === 'triggered' ? 'warning' : 'default'}>
              {t(`tradeDesk.holdings.status.${rule.status}` as UiTextKey)}
            </Badge>
            {rule.status === 'active'
              ? <Button size="xsm" variant="ghost" disabled={busyId === rule.id} aria-label={t('tradeDesk.holdings.pause')}
                onClick={() => void run(rule, () => tradeDeskApi.updateHoldingRule(rule.id, { status: 'paused' }))}><Pause className="h-4 w-4" /></Button>
              : <Button size="xsm" variant="ghost" disabled={busyId === rule.id} aria-label={t('tradeDesk.holdings.rearm')}
                onClick={() => void run(rule, () => tradeDeskApi.updateHoldingRule(rule.id, { status: 'active' }))}><Play className="h-4 w-4" /></Button>}
            <Button size="xsm" variant="ghost" disabled={busyId === rule.id} aria-label={t('tradeDesk.holdings.delete')}
              onClick={() => void run(rule, () => tradeDeskApi.deleteHoldingRule(rule.id))}><Trash2 className="h-4 w-4" /></Button>
          </div>
        </li>
      ))}
    </ul>
  );
}

function OptionCard({ position, rules, adding, onAdd, onChanged, onCancel }: {
  position: HoldingOption; rules: HoldingRule[]; adding: boolean; onAdd: () => void; onChanged: (warning?: string) => void; onCancel: () => void;
}) {
  const { t } = useUiLanguage();
  const expiry = position.expiry.slice(5).replace('-', '/');
  const urgent = position.daysLeft <= 2;
  return (
    <Card variant="bordered" padding="md">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-base font-semibold text-foreground"><span className="font-mono">{position.underlying}</span> {expiry} {position.label}</h3>
          <p className="mt-1 text-xs text-secondary-text">
            {t('tradeDesk.holdings.underlying')} {money(position.underlyingPrice)}
          </p>
        </div>
        <Badge variant={position.expired ? 'default' : urgent ? 'danger' : position.daysLeft <= 5 ? 'warning' : 'info'}>
          {position.expired ? t('tradeDesk.holdings.expired')
            : position.daysLeft === 0 ? t('tradeDesk.holdings.expiresToday') : `${position.daysLeft} ${t('tradeDesk.holdings.tradingDaysLeft')}`}
        </Badge>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div><dt className="text-xs text-muted-text">{t('tradeDesk.holdings.cost')}</dt><dd className="text-foreground">{money(position.cost)}</dd></div>
        <div><dt className="text-xs text-muted-text">{t('tradeDesk.holdings.value')}</dt><dd className="text-foreground">{money(position.value)}</dd></div>
        <div><dt className="text-xs text-muted-text">{t('tradeDesk.holdings.pnl')}</dt><dd className={tone(position.pnlPct)}>{pct(position.pnlPct)}</dd></div>
        <div><dt className="text-xs text-muted-text">{t('tradeDesk.holdings.ofMax')}</dt><dd className="text-foreground">{position.pctOfMax == null || position.pctOfMax <= 0 ? '-' : `${position.pctOfMax.toFixed(0)}%`}</dd></div>
      </dl>
      <table className="mt-3 w-full text-xs">
        <thead className="text-muted-text"><tr><th className="text-left font-normal">{t('tradeDesk.contract')}</th><th className="text-right font-normal">{t('tradeDesk.quantity')}</th><th className="text-right font-normal">{t('tradeDesk.holdings.avgCost')}</th><th className="text-right font-normal">{t('tradeDesk.holdings.mark')}</th></tr></thead>
        <tbody>
          {position.legs.map((leg) => (
            <tr key={leg.code} className="text-secondary-text">
              <td className="font-mono">{leg.qty < 0 ? 'short' : 'long'} {leg.strike}{leg.right === 'call' ? 'C' : 'P'}</td>
              <td className="text-right">{leg.qty}</td>
              <td className="text-right">{money(leg.averageCost)}</td>
              <td className="text-right">{money(leg.mark)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <RuleList rules={rules} onChanged={onChanged} />
      {adding
        ? <RuleForm target={{ positionKey: position.key, ticker: position.underlying, isOption: true, label: `${position.underlying} ${expiry} ${position.label}` }} onSaved={onChanged} onCancel={onCancel} />
        : <Button className="mt-3" size="sm" variant="ghost" onClick={onAdd}><Plus className="h-4 w-4" />{t('tradeDesk.holdings.addAlert')}</Button>}
    </Card>
  );
}

function StockRows({ stocks, rulesFor, adding, onAdd, onChanged, onCancel }: {
  stocks: HoldingStock[]; rulesFor: (key: string) => HoldingRule[]; adding: string | null;
  onAdd: (key: string) => void; onChanged: (warning?: string) => void; onCancel: () => void;
}) {
  const { t } = useUiLanguage();
  return (
    <>
    <ul className="space-y-3 md:hidden">
      {stocks.map((stock) => {
        const rules = rulesFor(stock.key);
        return (
          <li key={stock.key} className="rounded-xl bg-elevated/40 p-3 text-sm">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <span className="font-mono font-semibold text-foreground">{stock.ticker}</span>
                <p className="truncate text-xs text-secondary-text">{stock.name}</p>
              </div>
              <span className={`shrink-0 font-medium ${tone(stock.pnlPct)}`}>{pct(stock.pnlPct)}</span>
            </div>
            <dl className="mt-2 grid grid-cols-3 gap-2 text-xs">
              <div><dt className="text-muted-text">{t('tradeDesk.quantity')}</dt><dd className="text-foreground">{stock.qty}</dd></div>
              <div><dt className="text-muted-text">{t('tradeDesk.price')}</dt><dd className="text-foreground">{money(stock.price)}</dd></div>
              <div><dt className="text-muted-text">{t('tradeDesk.holdings.weight')}</dt><dd className="text-foreground">{stock.weightPct == null ? '-' : `${stock.weightPct.toFixed(1)}%`}</dd></div>
              <div><dt className="text-muted-text">{t('tradeDesk.holdings.avgCost')}</dt><dd className="text-foreground">{money(stock.averageCost)}</dd></div>
              <div><dt className="text-muted-text">{t('tradeDesk.holdings.value')}</dt><dd className="text-foreground">{money(stock.value, 0)}</dd></div>
            </dl>
            <RuleList rules={rules} onChanged={onChanged} />
            {adding === stock.key
              ? <RuleForm target={{ positionKey: stock.key, ticker: stock.ticker, isOption: false, label: `${stock.ticker} ${t('tradeDesk.holdings.shares')}` }} onSaved={onChanged} onCancel={onCancel} />
              : <Button className="mt-2" size="sm" variant="ghost" onClick={() => onAdd(stock.key)}><Plus className="h-4 w-4" />{t('tradeDesk.holdings.addAlert')}</Button>}
          </li>
        );
      })}
    </ul>
    <div className="hidden overflow-x-auto md:block">
      <table className="w-full min-w-[640px] text-sm">
        <thead className="text-xs text-muted-text">
          <tr>
            <th className="py-2 text-left font-normal">{t('tradeDesk.holdings.stock')}</th>
            <th className="text-right font-normal">{t('tradeDesk.quantity')}</th>
            <th className="text-right font-normal">{t('tradeDesk.holdings.avgCost')}</th>
            <th className="text-right font-normal">{t('tradeDesk.price')}</th>
            <th className="text-right font-normal">{t('tradeDesk.holdings.value')}</th>
            <th className="text-right font-normal">{t('tradeDesk.holdings.weight')}</th>
            <th className="text-right font-normal">{t('tradeDesk.holdings.pnl')}</th>
            <th className="text-right font-normal">{t('tradeDesk.holdings.alerts')}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/40">
          {stocks.map((stock) => {
            const rules = rulesFor(stock.key);
            return (
              <tr key={stock.key} className="align-top">
                <td className="py-2">
                  <span className="font-mono font-semibold text-foreground">{stock.ticker}</span>
                  <span className="ml-2 text-xs text-secondary-text">{stock.name}</span>
                  <RuleList rules={rules} onChanged={onChanged} />
                  {adding === stock.key
                    ? <RuleForm target={{ positionKey: stock.key, ticker: stock.ticker, isOption: false, label: `${stock.ticker} ${t('tradeDesk.holdings.shares')}` }} onSaved={onChanged} onCancel={onCancel} />
                    : null}
                </td>
                <td className="py-2 text-right">{stock.qty}</td>
                <td className="py-2 text-right">{money(stock.averageCost)}</td>
                <td className="py-2 text-right">{money(stock.price)}</td>
                <td className="py-2 text-right">{money(stock.value, 0)}</td>
                <td className="py-2 text-right">{stock.weightPct == null ? '-' : `${stock.weightPct.toFixed(1)}%`}</td>
                <td className={`py-2 text-right ${tone(stock.pnlPct)}`}>{pct(stock.pnlPct)}</td>
                <td className="py-2 text-right">
                  <Button size="xsm" variant="ghost" onClick={() => onAdd(stock.key)} aria-label={t('tradeDesk.holdings.addAlert')}>
                    <Plus className="h-4 w-4" />{rules.length || ''}
                  </Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
    </>
  );
}

export const HoldingsPanel: React.FC = () => {
  const { t } = useUiLanguage();
  const [data, setData] = useState<HoldingsResponse | null>(null);
  const [problem, setProblem] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [adding, setAdding] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await tradeDeskApi.getHoldings());
      setProblem('');
    } catch (error) {
      setProblem(errorText(error));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const refresh = async () => {
    setRefreshing(true);
    try {
      setData(await tradeDeskApi.refreshHoldings());
      setProblem('');
    } catch (error) {
      setProblem(errorText(error));
    } finally {
      setRefreshing(false);
    }
  };

  const [notice, setNotice] = useState('');
  const changed = (warning?: string) => { setAdding(null); setNotice(warning ?? ''); void load(); };
  const rules = data?.rules ?? [];
  const view = data?.view;
  const heldKeys = new Set([...(view?.stocks ?? []).map((row) => row.key), ...(view?.options ?? []).map((row) => row.key)]);
  const rulesFor = (key: string) => rules.filter((rule) => rule.positionKey === key);
  const otherRules = rules.filter((rule) => !rule.positionKey || !heldKeys.has(rule.positionKey));

  if (!data && !problem) return <Loading />;
  if (data && !data.enabled) {
    return <EmptyState icon={<Wallet className="h-8 w-8" />} title={t('tradeDesk.holdings.disabledTitle')} description={t('tradeDesk.holdings.disabledHint')} />;
  }
  return (
    <div className="space-y-5">
      <InlineAlert variant="info" message={t('tradeDesk.holdings.readOnly')} />
      {problem ? <InlineAlert variant="danger" message={problem} /> : null}
      {data?.error ? <InlineAlert variant="warning" message={`${t('tradeDesk.holdings.syncProblem')} (${data.error}). ${t('tradeDesk.holdings.syncHint')}`} /> : null}
      {notice ? <InlineAlert variant="warning" message={notice} action={<Button size="sm" variant="ghost" onClick={() => setNotice('')}>{t('tradeDesk.close')}</Button>} /> : null}
      <Card variant="gradient" padding="md">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.holdings.account')} {view?.account ?? ''} <span className="text-sm font-normal text-secondary-text">{view?.accountType ?? ''}</span></h2>
            <p className="mt-1 text-xs text-secondary-text">
              {view?.syncedAt
                ? `${t('tradeDesk.holdings.total')} ${money(view.totalAssets, 0)} · ${t('tradeDesk.holdings.cash')} ${money(view.cash, 0)} · ${t('tradeDesk.holdings.synced')} ${new Date(view.syncedAt).toLocaleString()}`
                : t('tradeDesk.holdings.neverSynced')}
            </p>
          </div>
          <Button size="sm" variant="ghost" onClick={() => void refresh()} isLoading={refreshing}><RefreshCw className="h-4 w-4" />{t('tradeDesk.holdings.sync')}</Button>
        </div>
      </Card>
      {view?.syncedAt ? <>
      <SummaryCard summary={data?.summary} onBuilt={(summary) => setData((current) => (current ? { ...current, summary } : current))} />
      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-secondary-text">{t('tradeDesk.holdings.options')}</h2>
        {view?.options.length ? (
          <div className="grid gap-4 xl:grid-cols-2">
            {view.options.map((position) => (
              <OptionCard key={position.key} position={position} rules={rulesFor(position.key)} adding={adding === position.key}
                onAdd={() => setAdding(position.key)} onChanged={changed} onCancel={() => setAdding(null)} />
            ))}
          </div>
        ) : <p className="text-sm text-secondary-text">{t('tradeDesk.holdings.noOptions')}</p>}
      </section>
      <Card variant="bordered" padding="md">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-secondary-text">{t('tradeDesk.holdings.stocks')}</h2>
        {view?.stocks.length
          ? <StockRows stocks={view.stocks} rulesFor={rulesFor} adding={adding} onAdd={setAdding} onChanged={changed} onCancel={() => setAdding(null)} />
          : <p className="text-sm text-secondary-text">{t('tradeDesk.holdings.noStocks')}</p>}
      </Card>
      </> : null}
      <Card variant="bordered" padding="md">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-secondary-text">{t('tradeDesk.holdings.otherAlerts')}</h2>
          {adding === '__ticker__' ? null : <Button size="sm" variant="ghost" onClick={() => setAdding('__ticker__')}><Plus className="h-4 w-4" />{t('tradeDesk.holdings.tickerAlert')}</Button>}
        </div>
        {adding === '__ticker__' ? <TickerRuleForm onSaved={changed} onCancel={() => setAdding(null)} /> : null}
        {otherRules.length ? <RuleList rules={otherRules} onChanged={changed} withTarget /> : <p className="mt-2 text-xs text-muted-text"><BellOff className="mr-1 inline h-3 w-3" />{t('tradeDesk.holdings.noOtherAlerts')}</p>}
        <p className="mt-4 text-xs text-muted-text">{t('tradeDesk.holdings.defaults')}</p>
      </Card>
    </div>
  );
};

function TickerRuleForm({ onSaved, onCancel }: { onSaved: (warning?: string) => void; onCancel: () => void }) {
  const { t } = useUiLanguage();
  const [ticker, setTicker] = useState('');
  const symbol = ticker.trim().toUpperCase();
  return (
    <div className="mt-2">
      <label className="block max-w-xs text-xs text-secondary-text">
        {t('tradeDesk.holdings.ticker')}
        <input value={ticker} onChange={(event) => setTicker(event.target.value)} placeholder="SPY" className={inputClass} />
      </label>
      {/^[A-Z][A-Z.-]{0,9}$/.test(symbol)
        ? <RuleForm key={symbol} target={{ positionKey: null, ticker: symbol, isOption: false, label: symbol }} onSaved={onSaved} onCancel={onCancel} />
        : <Button className="mt-2" size="sm" variant="ghost" onClick={onCancel}><X className="h-4 w-4" />{t('tradeDesk.close')}</Button>}
    </div>
  );
}

export default HoldingsPanel;
