import { useEffect, useState } from 'react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Card } from '../common';
import type { StopRisk } from '../../types/tradeDesk';

const pct = (value?: number | null, digits = 2) => (value == null ? '—' : `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(digits)}%`);
const tone = (value?: number | null) => (value != null && value < 0 ? 'hsl(var(--color-danger))' : undefined);

/** Loss at each plan stop, at all of them, and in a gap through them: percentages of the account only. */
export function StopRiskCard({ ticker, syncedAt }: { ticker?: string; syncedAt?: string | null }) {
  const [risk, setRisk] = useState<StopRisk | null>(null);
  useEffect(() => {
    const load = async () => {
      try { setRisk(await tradeDeskApi.getStopRisk()); } catch { setRisk(null); }  // no broker account: nothing to show
    };
    void load();
  }, [syncedAt]);
  if (!risk) return null;
  const rows = ticker ? risk.rows.filter((row) => row.ticker === ticker) : risk.rows;
  if (ticker && !rows.length) return null;
  const gap = `${risk.gapMultiple}×`;
  return (
    <Card variant="bordered" padding="sm" data-testid="stop-risk">
      <h3 className="px-1 pb-1 text-sm font-semibold text-foreground">{ticker ? `Risk at your ${ticker} stops` : 'Risk at your stops'}</h3>
      <div className="flex flex-wrap gap-2 px-1 text-xs">
        <span className="rounded-lg bg-elevated/60 px-2.5 py-1.5"><span className="text-secondary-text">If every stop is hit </span>
          <strong style={{ color: tone(risk.totals.atStopsPct) }}>{pct(risk.totals.atStopsPct)}</strong><span className="text-secondary-text"> of the account</span></span>
        <span className="rounded-lg bg-elevated/60 px-2.5 py-1.5"><span className="text-secondary-text">If each gaps through its stop to {gap} the distance </span>
          <strong style={{ color: tone(risk.totals.atGapsPct) }}>{pct(risk.totals.atGapsPct)}</strong></span>
      </div>
      {ticker ? <ul className="mt-2 space-y-1 px-1 text-xs">{rows.map((row) => (  // the chart's narrow side panel
        <li key={row.key} className="border-t border-border/30 pt-1">
          <p className="text-foreground">{row.label} <span className="text-muted-text">· {row.weightPct == null ? '—' : `${row.weightPct.toFixed(1)}%`} of the account</span></p>
          <p className="font-mono text-secondary-text">{row.unavailable ? row.unavailable : <>stop {row.stop}{row.stopDistancePct != null ? ` (${pct(row.stopDistancePct, 1)})` : ''} · at stop{' '}
            <span style={{ color: tone(row.atStopPct) }}>{row.pastStop ? 'past it' : pct(row.atStopPct)}</span> · gap <span style={{ color: tone(row.atGapPct) }}>{pct(row.atGapPct)}</span></>}</p>
        </li>))}</ul> : <div className="mt-2 overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs">
        <thead className="text-muted-text"><tr><th className="px-1 py-1 font-normal">Position</th><th className="px-1 py-1 text-right font-normal">Weight</th>
          <th className="px-1 py-1 text-right font-normal">Stop</th><th className="px-1 py-1 text-right font-normal">At stop</th><th className="px-1 py-1 text-right font-normal">Gap</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={row.key} className="border-t border-border/30">
          <td className="px-1 py-1 text-foreground">{row.label}</td>
          <td className="px-1 py-1 text-right font-mono">{row.weightPct == null ? '—' : `${row.weightPct.toFixed(1)}%`}</td>
          <td className="px-1 py-1 text-right font-mono">{row.stop == null ? '—' : `${row.stop}${row.stopDistancePct != null ? ` (${pct(row.stopDistancePct, 1)})` : ''}`}</td>
          {row.unavailable ? <td colSpan={2} className="px-1 py-1 text-right text-muted-text">{row.unavailable}</td> : <>
            <td className="px-1 py-1 text-right font-mono" style={{ color: tone(row.atStopPct) }}>{row.pastStop ? 'past it' : pct(row.atStopPct)}</td>
            <td className="px-1 py-1 text-right font-mono" style={{ color: tone(row.atGapPct) }}>{pct(row.atGapPct)}</td></>}
        </tr>)}</tbody>
      </table></div>}
      <p className="px-1 pt-1 text-[11px] text-muted-text">An instant move at today's prices, options at their current implied volatility. A position already past its stop would sell now, so it adds nothing more.
        {risk.withoutStop.length ? ` No stop: ${risk.withoutStop.join(', ')}.` : ''}{risk.unpriced.length ? ` Not priced: ${risk.unpriced.join(', ')}.` : ''} Totals leave these out.</p>
    </Card>
  );
}
