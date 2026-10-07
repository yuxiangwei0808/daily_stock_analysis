import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Button, Card } from '../common';
import type { PortfolioRisk as PortfolioRiskData } from '../../types/tradeDesk';

const money = (value: number | null | undefined, digits = 0) => (value == null ? '—'
  : `${value < 0 ? '−' : ''}$${Math.abs(value).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`);
const signed = (value: number | null | undefined, digits = 1) => (value == null ? '' : `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(digits)}%`);
const tone = (value: number | null | undefined) => (value == null ? undefined : value < 0 ? 'hsl(var(--color-danger))' : value > 0 ? 'hsl(var(--color-success))' : undefined);

/** Delta, time decay, beta and index-move P&L across your holdings (read-only estimates). */
export function PortfolioRisk({ syncedAt }: { syncedAt?: string | null }) {
  const [risk, setRisk] = useState<PortfolioRiskData | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true);
    try { setRisk(await tradeDeskApi.getHoldingsRisk()); setError(''); }
    catch (loadError) { setRisk(null); setError(loadError instanceof Error ? loadError.message : 'Risk is unavailable'); }
    finally { setBusy(false); }
  }, []);
  useEffect(() => { void load(); }, [load, syncedAt]);

  return (
    <Card variant="bordered" padding="md">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-secondary-text">Portfolio risk</h2>
        <Button size="sm" variant="ghost" onClick={() => void load()} isLoading={busy}><RefreshCw className="h-4 w-4" />Recalculate</Button>
      </div>
      {error ? <p className="mt-2 text-xs text-secondary-text">{error}</p> : null}
      {risk ? (
        <div data-testid="portfolio-risk">
          {risk.complete === false ? <p role="status" className="mt-2 text-xs text-secondary-text">Totals leave out {risk.unavailableTickers?.join(', ')}: no usable option price (no quote, a mark below intrinsic, or a spread too wide to trust).</p> : null}
          <div className="mt-3 flex flex-wrap gap-2 text-xs">
            {risk.scenarios.map((item) => (
              <span key={item.key} className="rounded-lg bg-elevated/60 px-2.5 py-1.5">
                <span className="text-secondary-text">If {item.label} </span>
                <strong style={{ color: tone(item.pnl) }}>{money(item.pnl)}</strong>
                {item.pct != null ? <span className="text-secondary-text"> ({signed(item.pct)})</span> : null}
              </span>
            ))}
          </div>
          <p className="mt-2 text-xs text-secondary-text">
            Net delta {money(risk.totals.deltaDollars)} · SPY-beta-weighted {money(risk.totals.spyBetaDollars)}
            {risk.totals.spyBetaPct != null ? ` (${signed(risk.totals.spyBetaPct, 0)} of the account)` : ''} · time decay{' '}
            <span style={{ color: tone(risk.totals.thetaPerDay) }}>{money(risk.totals.thetaPerDay, 2)}/day</span>
          </p>
          <div className="mt-3 overflow-x-auto rounded-xl border border-border/50">
            <table className="w-full whitespace-nowrap text-left text-xs">
              <thead className="bg-elevated/50 text-secondary-text">
                <tr><th className="px-3 py-2">Ticker</th><th className="px-3 py-2 text-right">Delta (shares)</th><th className="px-3 py-2 text-right">Delta $</th>
                  <th className="px-3 py-2 text-right">Beta SPY</th><th className="hidden px-3 py-2 text-right sm:table-cell">Beta QQQ</th><th className="px-3 py-2 text-right">Decay/day</th></tr>
              </thead>
              <tbody>
                {risk.rows.map((row) => (
                  <tr key={row.ticker} className="border-t border-border/40">
                    <td className="px-3 py-1.5 font-mono text-foreground">{row.ticker}</td>
                    <td className="px-3 py-1.5 text-right font-mono">{row.sharesEquiv == null ? '—' : `${row.sharesEquiv < 0 ? '−' : ''}${Math.abs(row.sharesEquiv).toFixed(0)}`}</td>
                    <td className="px-3 py-1.5 text-right font-mono" style={{ color: tone(row.deltaDollars) }}>{money(row.deltaDollars)}</td>
                    <td className="px-3 py-1.5 text-right font-mono">{row.betaAssumed ? '1.00*' : row.betaSpy?.toFixed(2)}</td>
                    <td className="hidden px-3 py-1.5 text-right font-mono sm:table-cell">{row.betaQqq == null ? '1.00*' : row.betaQqq.toFixed(2)}</td>
                    <td className="px-3 py-1.5 text-right font-mono" style={{ color: tone(row.thetaPerDay) }}>{row.thetaPerDay ? `${money(row.thetaPerDay, 2)}${row.thetaPartial ? '*' : ''}` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-[11px] text-muted-text">Estimates: each option's volatility is read from its price and held fixed; an index move shifts each holding by its one-year beta.
            {risk.rows.some((row) => row.betaAssumed || row.betaQqqAssumed || row.thetaPartial) ? ' * beta assumed 1.0 where index history is insufficient. Missing option prices leave exposure unavailable.' : ''}</p>
        </div>
      ) : null}
    </Card>
  );
}
