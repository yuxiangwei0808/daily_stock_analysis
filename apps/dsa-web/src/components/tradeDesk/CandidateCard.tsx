import { AlertTriangle, Star } from 'lucide-react';
import { Badge } from '../common';
import type { StrategyCandidate, TradeAdviceJob, TradeQuoteSnapshot } from '../../types/tradeDesk';
import { PayoffPanel } from './PayoffPanel';
import { formatDate, formatQuoteAge, textValue } from './deskFormat';
import { REDUNDANT_SCENARIOS, costText, dayLabel, expiryOf, humanKey, humanValue, money, popText } from './answerFormat';
import type { SharedNotes } from './answerFormat';

const CONFIDENCE: Record<string, 'success' | 'warning' | 'default'> = { high: 'success', medium: 'warning', low: 'default' };

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="min-w-0 rounded-xl bg-elevated/50 px-3 py-2">
      <div className="text-xs text-secondary-text">{label}</div>
      <div className="mt-0.5 font-mono text-sm font-semibold text-foreground [overflow-wrap:anywhere] sm:text-base">{value}</div>
      {note ? <div className="mt-0.5 text-xs text-muted-text [overflow-wrap:anywhere]">{note}</div> : null}
    </div>
  );
}

function Bullets({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <div>
      <h5 className="text-xs font-semibold text-secondary-text">{title}</h5>
      <ul className="mt-1 space-y-1 text-sm leading-6 text-foreground">{items.map((item) => <li key={item}>• {item}</li>)}</ul>
    </div>
  );
}

/** One candidate in full: the desk's take on it, key numbers, legs, payoff, then the model details folded away. */
export function CandidateCard({ candidate, advice, shared }: {
  candidate: StrategyCandidate; advice: TradeAdviceJob; shared?: SharedNotes;
}) {
  const payoff = candidate.payoff;
  const probability = candidate.probability;
  const snapshot: TradeQuoteSnapshot | null = advice.snapshots?.[candidate.snapshotId] || advice.snapshot || null;
  const quoteTime = snapshot?.quotedAt || snapshot?.receivedAt || null;
  const trigger = advice.triggers?.[candidate.id];
  const reviewed = Boolean(trigger);  // the model wrote its own take on this one
  const picked = reviewed && advice.assessment !== 'wait';
  const own = (items: string[] | undefined, common?: Set<string>) => (items || []).filter((item) => !common?.has(item));
  const warnings = own(candidate.warnings, shared?.warnings);
  const entry = own(candidate.entryConditions, shared?.entry);
  const exit = own(candidate.exitConditions, shared?.exit);
  const invalidation = candidate.invalidation && !shared?.invalidation.has(candidate.invalidation) ? candidate.invalidation : '';
  // A reviewed candidate's reasons are the model's own words; otherwise they restate the strategy and the view.
  const why = reviewed ? candidate.reasons.filter(Boolean) : [];
  // A long-side setup (trigger "above") is wrong below its invalidation level, a short-side one above it.
  const longSide = trigger?.triggerDirection !== 'below';
  const levels = trigger ? [
    trigger.triggerPrice != null ? `Enter ${longSide ? 'above' : 'below'} ${trigger.triggerPrice.toFixed(2)}` : '',
    trigger.invalidationPrice != null ? `Wrong ${longSide ? 'below' : 'above'} ${trigger.invalidationPrice.toFixed(2)}` : '',
    trigger.targetPrice != null ? `Target ${trigger.targetPrice.toFixed(2)}` : '',
    trigger.exitAt ? `Exit by ${dayLabel(trigger.exitAt)}` : '',
  ].filter(Boolean) : [];
  const scenarios = (candidate.scenarios || []).filter((item) => !REDUNDANT_SCENARIOS.has(String(item.kind)));
  const assumptions = Object.entries(probability.assumptions || {});
  const expiry = expiryOf(candidate);
  const contracts = candidate.legs.filter((leg) => leg.right !== 'stock').length;

  return (
    <section className="mt-4 rounded-2xl border border-border/50 bg-card/30 p-3 sm:p-4" data-testid="candidate-detail">
      <div className="flex flex-wrap items-center gap-2">
        {picked ? <Star className="h-4 w-4 fill-current text-cyan" aria-hidden="true" /> : null}
        <h3 className="text-base font-semibold text-foreground">{candidate.title}</h3>
        {picked ? <Badge variant="info">Picked by the desk</Badge> : null}
        <Badge variant={CONFIDENCE[candidate.evidenceConfidence] || 'default'}>{candidate.evidenceConfidence} confidence</Badge>
      </div>
      <p className="mt-1 text-xs text-muted-text">
        Quotes {formatDate(quoteTime)} ({formatQuoteAge(quoteTime)} ago){snapshot?.provider ? ` · ${snapshot.provider}` : ''}{snapshot?.stale ? ' · stale' : ''}
      </p>

      {why.length ? <div className="mt-3 rounded-xl border border-cyan/25 bg-cyan/5 p-3 text-sm leading-6 text-foreground" data-testid="candidate-why"><p className="mb-1 text-xs font-semibold text-cyan">The desk's take</p>{why.map((reason) => <p key={reason}>{reason}</p>)}</div> : null}
      {levels.length ? <p className="mt-2 flex flex-wrap gap-2 text-xs" data-testid="candidate-levels">{levels.map((level) => <span key={level} className="rounded-lg border border-border/60 px-2 py-1 text-foreground">{level}</span>)}</p> : null}

      <div className="mt-3 grid grid-cols-2 gap-2 lg:grid-cols-4">
        <Stat label="Cost" value={costText(candidate)} note={`fees ${money(payoff.fees)}${candidate.quantityForAllocation != null ? ` · ${candidate.quantityForAllocation}× fits your allocation` : ''}`} />
        <Stat label="Chance of profit" value={popText(candidate)} note={probability.available ? `at ${probability.horizonLabel}, model estimate` : textValue(probability.reason)} />
        <Stat label="Expires" value={dayLabel(expiry)} note={`${contracts} option leg${contracts === 1 ? '' : 's'}`} />
        <Stat label="Capital needed" value={money(payoff.capitalRequired)} note={payoff.capitalNote || undefined} />
      </div>

      <ul className="mt-3 divide-y divide-border/40 rounded-xl border border-border/50 text-sm" data-testid="candidate-legs">
        {candidate.legs.map((leg) => (
          <li key={`${leg.contractId}-${leg.side}`} className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 px-3 py-2">
            <span className="text-foreground">
              <span className={leg.side === 'buy' ? 'text-success' : 'text-danger'}>{leg.side === 'buy' ? 'Buy' : 'Sell'}</span>{' '}
              {leg.quantity}{leg.right === 'stock' ? ' shares' : ` × ${leg.strike?.toFixed(2) ?? '—'} ${leg.right}`}
              {leg.right !== 'stock' && leg.expiry ? <span className="text-secondary-text"> · {dayLabel(leg.expiry)}</span> : null}
              {leg.existing ? <span className="text-secondary-text"> · already held</span> : null}
            </span>
            <span className="font-mono text-foreground">{money(leg.entryPrice)}<span className="ml-2 text-xs text-muted-text">{leg.contractId}</span></span>
          </li>
        ))}
      </ul>

      <div className="mt-4"><PayoffPanel candidate={candidate} spot={snapshot?.spot ?? null} /></div>

      {entry.length || exit.length || invalidation ? (
        <div className="mt-4 grid gap-4 border-t border-border/50 pt-4 md:grid-cols-3" data-testid="candidate-plan">
          <Bullets title="Enter when" items={entry} />
          <Bullets title="Exit when" items={exit} />
          {invalidation ? <Bullets title="Wrong if" items={[invalidation]} /> : null}
        </div>
      ) : null}
      {warnings.length ? (
        <ul className="mt-3 space-y-1 text-xs leading-5 text-warning" data-testid="candidate-warnings">
          {warnings.map((warning) => <li key={warning}><AlertTriangle className="mr-1 inline h-3.5 w-3.5" />{warning}</li>)}
        </ul>
      ) : null}

      <details className="mt-4 rounded-xl border border-border/40 px-3 py-2 text-xs text-secondary-text">
        <summary className="cursor-pointer select-none font-medium text-secondary-text">Model details</summary>
        <div className="mt-2 space-y-3">
          {!reviewed && candidate.reasons.length ? <p>{candidate.reasons.join(' ')}</p> : null}
          {payoff.assignmentNote ? <p><strong className="text-foreground">Assignment:</strong> {payoff.assignmentNote}</p> : null}
          <p><strong className="text-foreground">Chance of profit:</strong> {textValue(probability.reason || probability.method)}</p>
          {assumptions.length ? (
            <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
              {assumptions.map(([key, value]) => (
                <div key={key} className="flex justify-between gap-3"><dt>{humanKey(key)}</dt><dd className="text-right text-foreground">{humanValue(key, value)}</dd></div>
              ))}
            </dl>
          ) : null}
          {scenarios.length ? (
            <div><strong className="text-foreground">Scenarios</strong>
              <ul className="mt-1 space-y-1">{scenarios.map((scenario, index) => (
                <li key={index}>{Object.entries(scenario).map(([key, value]) => `${humanKey(key)}: ${humanValue(key, value)}`).join(' · ')}</li>
              ))}</ul>
            </div>
          ) : null}
        </div>
      </details>
    </section>
  );
}
