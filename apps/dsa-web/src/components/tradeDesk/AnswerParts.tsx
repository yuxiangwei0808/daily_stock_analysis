import { useState } from 'react';
import { Badge } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { TradeAdviceJob, TradeDeskDataMode, TradeModelPanel } from '../../types/tradeDesk';
import { signedPct, textValue, formatDetailValue, modeLabel } from './deskFormat';

export function NxUsed({ job }: { job: TradeAdviceJob }) {
  const nx = job.nxTunnel;
  if (!nx) return null;
  const level = (value: number) => value.toFixed(2);
  return (
    <section className="mt-4 rounded-xl border border-border/40 bg-card/30 p-3 text-sm" data-testid="nx-used">
      <h3 className="text-sm font-semibold text-foreground">Your NX tunnel <span className="font-normal text-muted-text">(daily, as of {nx.asOf})</span></h3>
      <p className="mt-1 text-secondary-text">{nx.summary || `Fast ${level(nx.fast.bottom)}–${level(nx.fast.top)} (${nx.fast.state}), slow ${level(nx.slow.bottom)}–${level(nx.slow.top)} (${nx.slow.state})`}</p>
      <p className="mt-1 text-xs text-muted-text">Reference levels for stops and invalidation; NX has no tested edge on its own.</p>
    </section>
  );
}

export function ReferencesUsed({ job }: { job: TradeAdviceJob }) {
  const references = job.references ?? [];
  if (!references.length) return null;
  return (
    <section className="mt-4 rounded-xl border border-border/40 bg-card/30 p-3 text-sm" data-testid="references-used">
      {references.map((item) => (
        <div key={item.kind} className="mt-1 first:mt-0">
          <h3 className="text-sm font-semibold text-foreground">{item.title}</h3>
          <p className="mt-1 text-secondary-text">{item.summary}</p>
        </div>
      ))}
      <p className="mt-1 text-xs text-muted-text">Background only: neither social attention nor the hosts' calls has a tested edge yet; both are tracked in the weekly record.</p>
    </section>
  );
}

export function PositionUsed({ job }: { job: TradeAdviceJob }) {
  const position = job.position;
  if (!position) return null;
  const parts = [
    position.shares ? `${position.shares} shares${signedPct(position.stockPnlPct)}` : '',
    ...position.options.map((row) => `${row.label} ${row.expiry}${signedPct(row.pnlPct)} · ${row.daysLeft} trading days left`),
  ].filter(Boolean);
  const inputs = job.positionInputs;
  return (
    <section className="mt-4 rounded-xl border border-border/40 bg-card/30 p-3 text-sm" data-testid="position-used">
      <h3 className="text-sm font-semibold text-foreground">Your position (read-only)</h3>
      <p className="mt-1 text-secondary-text">{parts.join('; ')}</p>
      {inputs?.planFromPosition ? <p className="mt-1 text-xs text-muted-text">The "custom" candidate is your held options priced from the current mid (holding from here).</p> : null}
      {inputs?.existingShares ? <p className="mt-1 text-xs text-muted-text">{inputs.existingShares >= 100
        ? `Compared with your shares in mind: a protective put, and covered calls on ${Math.floor(inputs.existingShares / 100) * 100} of your ${inputs.existingShares} shares.`
        : `Compared with your ${inputs.existingShares} shares in mind: a protective put first (covered calls need 100 shares).`}</p> : null}
      {position.alerts.length ? <p className="mt-1 text-xs text-muted-text">Your alerts: {position.alerts.map((alert) => `${alert.kind.replace(/_/g, ' ')} ${alert.value}`).join(', ')}</p> : null}
    </section>
  );
}

export function MetricList({ values }: { values: Record<string, unknown> }) {
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

export function ModeBadge({ mode }: { mode: TradeDeskDataMode }) {
  const { t } = useUiLanguage();
  return <Badge variant={mode === 'live' ? 'info' : 'history'}>{modeLabel(mode, t)}</Badge>;
}

export function AdviceVerdict({ job }: { job: TradeAdviceJob }) {
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

export function ModelPanel({ panel }: { panel: TradeModelPanel }) {
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
