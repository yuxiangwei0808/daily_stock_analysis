import type React from 'react';
import { Check, ChevronDown, CircleDollarSign, FileText, SlidersHorizontal, Sparkles } from 'lucide-react';
import { Badge, Button, Card, InlineAlert } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { TradeDeskCatalogItem, TradeDeskHealth } from '../../types/tradeDesk';
import type { DecisionSignalItem } from '../../types/decisionSignals';
import { verdictSummary, liveIsBlocked } from './deskFormat';
import type { AdviceFormState } from './deskFormat';

export function AdviceForm({
  form,
  setForm,
  catalog,
  health,
  selectedStrategies,
  setSelectedStrategies,
  onSubmit,
  isSubmitting,
  sourceReportId,
  heldNote,
  reportVerdict,
  open,
  onToggle,
  tickerOptions,
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
  heldNote?: string;
  reportVerdict?: DecisionSignalItem | null;
  open: boolean;
  onToggle: () => void;
  tickerOptions: string[];
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
  // Filled-in extras stay visible as a count on the closed "More options" summary.
  const extras = [form.expiry, form.allocation, form.existingShares !== '0' && form.existingShares, form.marginPerUnit, form.planLegs.trim(), selectedStrategies.length].filter(Boolean).length;
  const field = 'input-surface h-10 w-full rounded-xl border px-3 text-sm text-foreground';
  const caption = 'mb-1.5 block text-xs font-medium text-secondary-text';

  return (
    <Card variant="gradient" padding="md">
      <button type="button" aria-expanded={open} onClick={onToggle} className="flex w-full items-center justify-between gap-3 text-left">
        <span className="flex min-w-0 items-center gap-2">
          <Sparkles className="h-4 w-4 shrink-0 text-cyan" />
          <span className="text-base font-semibold text-foreground">{t('tradeDesk.ask')}</span>
          {!open && form.ticker ? <span className="truncate font-mono text-sm text-secondary-text">{form.ticker}</span> : null}
        </span>
        <span className="flex shrink-0 items-center gap-2">
          {sourceReportId ? <Badge variant="info">Report #{sourceReportId}</Badge> : null}
          <ChevronDown className={`h-4 w-4 text-secondary-text transition-transform ${open ? 'rotate-180' : ''}`} />
        </span>
      </button>

      {open ? <div className="mt-4 space-y-4">
        <div className="grid gap-3 md:grid-cols-[180px_minmax(0,1fr)]">
          <label><span className={caption}>{t('tradeDesk.ticker')}</span><input aria-label={t('tradeDesk.ticker')} list="trade-desk-tickers" value={form.ticker} onChange={(event) => update('ticker', event.target.value.toUpperCase())} placeholder={t('tradeDesk.tickerPlaceholder')} className="input-surface input-focus-glow h-11 w-full rounded-xl border bg-transparent px-4 font-mono text-sm text-foreground" /><datalist id="trade-desk-tickers">{tickerOptions.map((ticker) => <option key={ticker} value={ticker} />)}</datalist></label>
          <label><span className={caption}>{t('tradeDesk.message')}</span><textarea aria-label={t('tradeDesk.message')} value={form.message} onChange={(event) => update('message', event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !disabled && !liveUnavailable && form.ticker.trim()) onSubmit(); }} placeholder={t('tradeDesk.questionPlaceholder')} rows={2} className="input-surface input-focus-glow w-full rounded-xl border px-4 py-2.5 text-sm text-foreground" /></label>
        </div>

        {reportVerdict ? <p className="flex items-start gap-2 text-xs text-secondary-text" data-testid="report-verdict" title={reportVerdict.reason || undefined}><FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-cyan" /><span><strong className="text-foreground">Latest report:</strong> {verdictSummary(reportVerdict)}{reportVerdict.reason ? <span className="block text-muted-text">{reportVerdict.reason.length > 160 ? `${reportVerdict.reason.slice(0, 157)}…` : reportVerdict.reason}</span> : null}</span></p> : null}

        {heldNote && form.dataMode === 'live' ? <label className="flex cursor-pointer items-start gap-2 rounded-xl border border-cyan/25 bg-cyan/5 p-3 text-sm" data-testid="use-holdings"><input type="checkbox" className="mt-1" checked={form.useHoldings} onChange={(event) => update('useHoldings', event.target.checked)} /><span><strong className="text-foreground">Use my position</strong><span className="block text-secondary-text">You hold {heldNote}. The answer weighs holding, closing, hedging or rolling it; owned shares count for covered calls.</span></span></label> : null}

        <div className="grid gap-3 md:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)]">
          <fieldset><legend className={caption}>Data</legend><div className="grid grid-cols-2 gap-2 text-sm">
            <label className={`flex cursor-pointer items-start gap-2 rounded-xl border p-2 ${form.dataMode === 'live' ? 'border-cyan/40 bg-cyan/5' : 'border-border/50'}`}><input type="radio" name="trade-desk-mode" className="mt-1" checked={form.dataMode === 'live'} onChange={() => update('dataMode', 'live')} /><span><strong className="block text-foreground">{t('tradeDesk.live')}</strong><span className="block text-xs text-secondary-text">{health?.live.available ? (health.live.message || 'Fresh verified quotes') : (health?.live.code === 'rights_unknown' ? 'Connected; quote permissions are unverified' : t('tradeDesk.liveUnavailable'))}</span></span></label>
            <label className={`flex cursor-pointer items-start gap-2 rounded-xl border p-2 ${form.dataMode === 'replay' ? 'border-cyan/40 bg-cyan/5' : 'border-border/50'}`}><input type="radio" name="trade-desk-mode" className="mt-1" checked={form.dataMode === 'replay'} onChange={() => update('dataMode', 'replay')} /><span><strong className="block text-foreground">{t('tradeDesk.replay')}</strong><span className="block text-xs text-secondary-text">{t('tradeDesk.replaySynthetic')}</span></span></label>
          </div></fieldset>
          <label><span className={caption}>{t('tradeDesk.direction')}</span><select aria-label={t('tradeDesk.direction')} value={form.direction} onChange={(event) => update('direction', event.target.value as AdviceFormState['direction'])} className={field}><option value="auto">{t('tradeDesk.direction.auto')}</option><option value="bullish">{t('tradeDesk.direction.bullish')}</option><option value="bearish">{t('tradeDesk.direction.bearish')}</option><option value="neutral">{t('tradeDesk.direction.neutral')}</option><option value="volatile">{t('tradeDesk.direction.volatile')}</option></select></label>
          <label><span className={caption}>{t('tradeDesk.horizon')}</span><select aria-label={t('tradeDesk.horizon')} value={form.horizon} onChange={(event) => update('horizon', event.target.value as AdviceFormState['horizon'])} className={field}><option value="both">{t('tradeDesk.horizon.both')}</option><option value="intraday">{t('tradeDesk.horizon.intraday')}</option><option value="swing">{t('tradeDesk.horizon.swing')}</option></select></label>
        </div>
        {liveUnavailable ? <InlineAlert variant="warning" title={t('tradeDesk.liveUnavailable')} message={health?.live.message || t('tradeDesk.staleData')} /> : null}
        {form.dataMode === 'replay' ? <InlineAlert variant="info" message={t('tradeDesk.replaySynthetic')} /> : null}

        <details className="rounded-2xl border border-border/50 bg-card/20 p-3">
          <summary className="cursor-pointer text-sm font-semibold text-foreground"><SlidersHorizontal className="mr-1.5 inline h-4 w-4 text-cyan" />{t('tradeDesk.moreOptions')}{extras ? <span className="ml-2 text-xs font-normal text-cyan">{extras} set</span> : null}</summary>
          <div className="mt-3 space-y-4">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <label><span className={caption}>{t('tradeDesk.expiry')}</span><input aria-label={t('tradeDesk.expiry')} type="date" value={form.expiry} onChange={(event) => update('expiry', event.target.value)} className={field} /></label>
              <label><span className={caption}>{t('tradeDesk.allocation')}</span><input aria-label={t('tradeDesk.allocation')} type="number" min="0" step="any" value={form.allocation} onChange={(event) => update('allocation', event.target.value)} placeholder="optional" className={field} /></label>
              <label><span className={caption}>{t('tradeDesk.existingShares')}</span><input aria-label={t('tradeDesk.existingShares')} type="number" min="0" step="1" value={form.existingShares} onChange={(event) => update('existingShares', event.target.value)} className={field} /></label>
              <label><span className={caption}>{t('tradeDesk.marginPerUnit')}</span><input aria-label={t('tradeDesk.marginPerUnit')} type="number" min="0" step="any" value={form.marginPerUnit} onChange={(event) => update('marginPerUnit', event.target.value)} placeholder="optional" className={field} /></label>
            </div>
            <div>
              <span className={caption}>{t('tradeDesk.strategy')}</span>
              <div className="flex flex-wrap gap-2">
                {catalog.length === 0 ? <span className="text-sm text-secondary-text">{t('tradeDesk.strategyAll')}</span> : catalog.map((item) => {
                  const selected = selectedStrategies.includes(item.id);
                  return <button key={item.id} type="button" onClick={() => toggleStrategy(item.id)} className={`rounded-full border px-3 py-1.5 text-xs transition ${selected ? 'border-cyan/50 bg-cyan/10 text-cyan' : 'border-border/60 text-secondary-text hover:text-foreground'}`} aria-pressed={selected}>{selected ? <Check className="mr-1 inline h-3 w-3" /> : null}{item.title || item.id}</button>;
                })}
              </div>
            </div>
            <label className="block"><span className={caption}>{t('tradeDesk.planLegs')}</span><textarea aria-label={t('tradeDesk.planLegs')} value={form.planLegs} onChange={(event) => update('planLegs', event.target.value)} placeholder={'buy 1 call 230 2026-10-16\nsell 1 call 240 2026-10-16'} rows={2} className="input-surface w-full rounded-xl border px-4 py-2.5 font-mono text-xs text-foreground" /><span className="mt-1 block text-xs text-secondary-text">{t('tradeDesk.planLegsHint')}</span></label>
            <div className="grid gap-3 sm:grid-cols-3">
              <label><span className={caption}>{t('tradeDesk.feePerContract')}</span><input aria-label={t('tradeDesk.feePerContract')} type="number" min="0" step="any" value={form.feePerContract} onChange={(event) => update('feePerContract', event.target.value)} className={field} /></label>
              <label><span className={caption}>{t('tradeDesk.riskFreeRate')}</span><input aria-label={t('tradeDesk.riskFreeRate')} type="number" step="any" value={form.riskFreeRate} onChange={(event) => update('riskFreeRate', event.target.value)} className={field} /></label>
              <label><span className={caption}>{t('tradeDesk.dividendYield')}</span><input aria-label={t('tradeDesk.dividendYield')} type="number" min="0" step="any" value={form.dividendYield} onChange={(event) => update('dividendYield', event.target.value)} className={field} /></label>
            </div>
          </div>
        </details>

        <div className="flex flex-col gap-3 border-t border-border/50 pt-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="max-w-2xl text-xs leading-5 text-secondary-text"><CircleDollarSign className="mr-1 inline h-3.5 w-3.5 text-cyan" />{t('tradeDesk.noAccountValue')}</p>
          <Button type="button" size="lg" disabled={disabled || liveUnavailable || !form.ticker.trim()} isLoading={isSubmitting} onClick={onSubmit}><Sparkles className="h-4 w-4" />{t('tradeDesk.getAdvice')}</Button>
        </div>
      </div> : null}
    </Card>
  );
}
