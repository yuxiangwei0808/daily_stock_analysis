import { AlertTriangle } from 'lucide-react';
import { Badge, Card } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { StrategyCandidate, TradeAdviceJob, TradeQuoteSnapshot } from '../../types/tradeDesk';
import { MetricList, ModeBadge } from './AnswerParts';
import { PayoffPanel } from './PayoffPanel';
import { formatMoney, formatNumber, formatDate, formatPercent, textValue, formatQuoteAge } from './deskFormat';

export function CandidateCard({ candidate, advice }: { candidate: StrategyCandidate; advice: TradeAdviceJob }) {
  const { t } = useUiLanguage();
  const payoff = candidate.payoff;
  const probability = candidate.probability;
  const snapshot: TradeQuoteSnapshot | null = advice.snapshots?.[candidate.snapshotId] || advice.snapshot || null;
  const quoteTime = snapshot?.quotedAt || snapshot?.receivedAt || null;
  const sensitivity = probability.sensitivity || [];
  return (
    <Card variant="bordered" padding="none" className="overflow-hidden p-3 sm:p-5">
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
      </div>

      <div className="mt-4">
        <h4 className="text-xs font-semibold uppercase tracking-[0.16em] text-secondary-text">{t('tradeDesk.legs')}</h4>
        <div className="mt-2 overflow-x-auto rounded-xl border border-border/50">
          <table className="w-full min-w-[620px] text-left text-xs">
            <thead className="bg-elevated/50 text-secondary-text"><tr><th className="px-3 py-2">{t('tradeDesk.contract')}</th><th className="px-3 py-2">{t('tradeDesk.side')}</th><th className="px-3 py-2">{t('tradeDesk.quantity')}</th><th className="px-3 py-2">Strike</th><th className="px-3 py-2">Expiry</th><th className="px-3 py-2">Entry</th></tr></thead>
            <tbody>{candidate.legs.map((leg) => <tr key={`${leg.contractId}-${leg.side}`} className="border-t border-border/40"><td className="px-3 py-2 font-mono text-foreground">{leg.contractId}<span className="ml-2 text-secondary-text">{leg.right}</span></td><td className="px-3 py-2 text-foreground">{leg.side}</td><td className="px-3 py-2 text-foreground">{leg.quantity} × {leg.multiplier}</td><td className="px-3 py-2 text-foreground">{formatNumber(leg.strike)}</td><td className="px-3 py-2 text-secondary-text">{leg.expiry?.slice(0, 10) || '—'}</td><td className="px-3 py-2 font-mono text-foreground">{formatMoney(leg.entryPrice)}</td></tr>)}</tbody>
          </table>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <div className="rounded-xl bg-elevated/50 p-3"><span className="text-xs text-secondary-text">{t('tradeDesk.cost')}</span><div className="mt-1 text-sm text-foreground">{payoff.entryDebit < 0 ? t('tradeDesk.credit') : t('tradeDesk.debit')} {formatMoney(Math.abs(payoff.entryDebit))} · Fees {formatMoney(payoff.fees)}</div><div className="mt-1 text-xs text-secondary-text">Capital {formatMoney(payoff.capitalRequired)} · {payoff.capitalNote || '—'}</div>{candidate.quantityForAllocation != null ? <div className="mt-1 text-xs text-secondary-text">{t('tradeDesk.quantityEstimate')}: {candidate.quantityForAllocation}</div> : null}</div>
          <div className="rounded-xl bg-elevated/50 p-3"><span className="text-xs text-secondary-text">Assignment</span><div className="mt-1 text-sm text-foreground">{payoff.assignmentNote || '—'}</div></div>
        </div>
      </div>

      <div className="mt-4 border-t border-border/50 pt-4">
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-[0.16em] text-secondary-text">{t('tradeDesk.payoff')}</h4>
        <PayoffPanel candidate={candidate} spot={snapshot?.spot ?? null} />
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

      <p className="mt-3 text-xs text-secondary-text">{candidate.reasons.slice(0, 2).join(' · ')}</p>
    </Card>
  );
}
