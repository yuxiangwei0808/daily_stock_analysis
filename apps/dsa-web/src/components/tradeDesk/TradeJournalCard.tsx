import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Button, Card } from '../common';
import type { TradeJournal, TradeJournalStats, TradeJournalTrip } from '../../types/tradeDesk';

const money = (value?: number | null) => (value == null ? '—'
  : `${value > 0 ? '+' : value < 0 ? '−' : ''}$${Math.abs(value).toLocaleString('en-US', { maximumFractionDigits: Math.abs(value) >= 100 ? 0 : 2 })}`);
const pct = (value?: number | null, signed = true) => (value == null ? '—' : `${signed && value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(signed ? 1 : 0)}%`);
const tone = (value?: number | null) => (value == null ? undefined : value > 0 ? 'hsl(var(--color-success))' : value < 0 ? 'hsl(var(--color-danger))' : undefined);

function StatsTable({ title, rows }: { title: string; rows: Array<TradeJournalStats & { label: string }> }) {
  const shown = rows.filter((row) => row.trades);
  if (!shown.length) return null;
  return (
    <div>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-secondary-text">{title}</h3>
      <table className="mt-1 w-full text-xs">
        <thead className="text-muted-text"><tr><th className="py-1 text-left font-normal" /><th className="pl-2 text-right font-normal">Trades</th>
          <th className="pl-2 text-right font-normal">Win</th><th className="pl-2 text-right font-normal">P&amp;L</th><th className="pl-2 text-right font-normal">Avg</th></tr></thead>
        <tbody className="divide-y divide-border/40">
          {shown.map((row) => (
            <tr key={row.label}>
              <td className="py-1 text-foreground">{row.label}</td>
              <td className="pl-2 text-right">{row.trades}</td>
              <td className="pl-2 text-right">{pct(row.winRate, false)}</td>
              <td className="whitespace-nowrap pl-2 text-right font-mono" style={{ color: tone(row.totalPnl) }}>{money(row.totalPnl)}</td>
              <td className="whitespace-nowrap pl-2 text-right font-mono" style={{ color: tone(row.avgReturnPct) }}>{pct(row.avgReturnPct)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const tripLine = (trip: TradeJournalTrip) => `${trip.ticker} ${trip.position} ${trip.kind === 'stock' ? 'stock' : `${trip.kind}s`} · ${trip.opened.slice(5, 10)}→${trip.closed.slice(5, 10)}`;

/** Your own round trips from moomoo fills (read-only): realized P&L by type, hold and system signal. */
export function TradeJournalCard() {
  const [journal, setJournal] = useState<TradeJournal | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    tradeDeskApi.getTradeJournal().then((result) => setJournal(result.journal)).catch(() => undefined);
  }, []);
  const refresh = async () => {
    setBusy(true); setError('');
    try { setJournal((await tradeDeskApi.refreshTradeJournal()).journal); }
    catch (refreshError) { setError(refreshError instanceof Error ? refreshError.message : 'Your fills could not be read'); }
    finally { setBusy(false); }
  };
  return (
    <Card variant="bordered" padding="md">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold text-foreground">Your trades</h2>
          <p className="text-xs text-secondary-text">From your moomoo fills over the last year (read-only){journal?.builtAt ? ` · read ${new Date(journal.builtAt).toLocaleString()}` : ''}</p>
        </div>
        <Button size="sm" variant="ghost" onClick={() => void refresh()} isLoading={busy}><RefreshCw className="h-4 w-4" />{journal ? 'Read fills again' : 'Read fills from moomoo'}</Button>
      </div>
      {error ? <p className="mt-2 text-xs text-secondary-text">{error}</p> : null}
      {journal ? (
        <div className="mt-3 space-y-4" data-testid="trade-journal">
          <p className="text-sm text-foreground">
            {journal.total.trades} round trips{journal.firstFill ? ` since ${journal.firstFill}` : ''} · realized{' '}
            <strong style={{ color: tone(journal.total.totalPnl) }}>{money(journal.total.totalPnl)}</strong> · win rate {pct(journal.total.winRate, false)}
            {journal.total.avgHoldDays != null ? ` · average hold ${journal.total.avgHoldDays} days` : ''}
          </p>
          <div className="grid gap-4 lg:grid-cols-3">
            <StatsTable title="By type" rows={journal.byType} />
            <StatsTable title="By holding time" rows={journal.byHold} />
            <StatsTable title="With the system's signals" rows={journal.bySignal} />
          </div>
          <div className="grid gap-4 sm:grid-cols-2 text-xs">
            {([['Best', journal.best], ['Worst', journal.worst]] as const).map(([title, trips]) => (
              <div key={title}>
                <h3 className="font-semibold uppercase tracking-wide text-secondary-text">{title}</h3>
                <ul className="mt-1 space-y-1">
                  {trips.map((trip) => (
                    <li key={`${trip.code}-${trip.opened}-${trip.closed}`} className="flex justify-between gap-2">
                      <span className="text-foreground">{tripLine(trip)}{trip.how !== 'closed' ? <span className="text-muted-text"> ({trip.how})</span> : null}</span>
                      <span className="font-mono" style={{ color: tone(trip.pnl) }}>{money(trip.pnl)} ({pct(trip.returnPct)})</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
          <p className="text-[11px] text-muted-text">Gross of commissions. Spreads count as their legs. {journal.openLotsNote} A system signal is a tracked trade idea, breakout, report call, social pick or YouTube call on the ticker in the five days before you opened.</p>
        </div>
      ) : null}
    </Card>
  );
}
