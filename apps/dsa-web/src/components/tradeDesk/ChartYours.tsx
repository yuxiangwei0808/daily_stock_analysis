import { useEffect, useState } from 'react';
import type { Chart } from 'klinecharts';
import { Square, Trash2 } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Badge, Button, Card } from '../common';
import type { ChartGradeKind, ChartGradeTable, ChartYours } from '../../types/tradeDesk';
import { nyTime, price } from './chartOverlays';
import { errorMessage } from './deskFormat';

const signedR = (value?: number | null) => (value == null ? '—' : `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(2)}R`);
const STATUS: Record<string, string> = { waiting: 'Waiting', open: 'Open', closed: 'Finished', expired: 'Expired', void: 'Void' };

function describe(item: ChartYours) {
  if (item.kind === 'box') {
    const box = `Box ${price(item.bottom ?? 0)}–${price(item.top ?? 0)}`;
    if (item.status === 'waiting') return item.armed ? `${box}: armed, ${item.direction} at ${price(item.price ?? 0)}` : `${box}: waits for the price to leave and come back`;
    if (item.status === 'expired') return `${box}: no return within five sessions`;
    return `${box}: ${item.direction} at ${price(item.price ?? 0)}, stop ${price(item.stop ?? 0)}, target ${price(item.target ?? 0)}`;
  }
  const levels = `stop ${price(item.stop ?? 0)}, target ${price(item.target ?? 0)}`;
  if (item.status === 'waiting') return `${item.direction} from the next bar's open, ${levels}`;
  if (item.status === 'void') return `${item.direction}: ${item.void}`;
  return `${item.direction} at ${price(item.price ?? 0)}${item.entryTime ? ` (${nyTime(item.entryTime)} ET)` : ''}, ${levels}`;
}

/** Your marks and kept boxes: recorded and graded like the system's flags. Nothing is sent to moomoo. */
export function YoursCard({ ticker, lastPrice, yours, chart, onChanged }: { ticker: string; lastPrice: number | null; yours: ChartYours[];
  chart: React.RefObject<Chart | null>; onChanged: () => void }) {
  const [direction, setDirection] = useState<'long' | 'short'>('long');
  const [stop, setStop] = useState('');
  const [target, setTarget] = useState('');
  const [draft, setDraft] = useState<{ top: number; bottom: number } | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const run = async (work: () => Promise<unknown>) => {
    setBusy(true); setError('');
    try { await work(); onChanged(); } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(false); }
  };
  const mark = () => {
    const stopValue = Number(stop);
    const targetValue = Number(target);
    if (!(stopValue > 0) || !(targetValue > 0)) { setError('Enter a stop and a target'); return; }
    void run(async () => { await tradeDeskApi.addChartMark(ticker, { direction, stop: stopValue, target: targetValue }); setStop(''); setTarget(''); });
  };
  const clearDraft = () => { chart.current?.removeOverlay({ groupId: 'draft' }); setDraft(null); setDrawing(false); };
  const draw = () => {
    const instance = chart.current;
    if (!instance) return;
    clearDraft();
    setDrawing(true);
    instance.createOverlay({ name: 'dsaDraftBox', groupId: 'draft', onDrawEnd: ({ overlay }) => {
      const values = overlay.points.map((point) => point.value).filter((value): value is number => typeof value === 'number');
      if (values.length >= 2) setDraft({ top: Math.max(...values), bottom: Math.min(...values) });
      setDrawing(false);
    } });
  };
  const keep = () => {
    if (!draft) return;
    void run(async () => { await tradeDeskApi.addChartBox(ticker, { top: Number(draft.top.toFixed(4)), bottom: Number(draft.bottom.toFixed(4)) }); clearDraft(); });
  };
  const side = direction === 'long' ? { stop: 'below', target: 'above' } : { stop: 'above', target: 'below' };

  return (
    <Card variant="bordered" padding="sm" data-testid="your-marks">
      <h3 className="px-1 pb-1 text-sm font-semibold text-foreground">Your marks</h3>
      <p className="px-1 pb-2 text-[11px] text-muted-text">A mark enters at the next 15-minute bar's open; a kept box is traded by the fade rule. Both follow the backtest's exits and are graded against the same trade at random times. Nothing is sent to moomoo.</p>
      <div className="flex flex-wrap items-end gap-2 px-1 text-xs">
        <div className="flex gap-1 rounded-lg border border-border/50 p-1" role="group" aria-label="Mark direction">
          {(['long', 'short'] as const).map((key) => <button key={key} type="button" aria-pressed={direction === key} onClick={() => setDirection(key)}
            className={`rounded-md px-2 py-0.5 ${direction === key ? 'bg-cyan/10 text-cyan' : 'text-secondary-text'}`}>{key === 'long' ? 'Long' : 'Short'}</button>)}
        </div>
        <label className="flex flex-col gap-0.5 text-secondary-text">Stop
          <input aria-label="Mark stop" inputMode="decimal" value={stop} onChange={(event) => setStop(event.target.value)} placeholder={side.stop}
            className="input-surface h-8 w-20 rounded-md border px-2 font-mono text-foreground" />
        </label>
        <label className="flex flex-col gap-0.5 text-secondary-text">Target
          <input aria-label="Mark target" inputMode="decimal" value={target} onChange={(event) => setTarget(event.target.value)} placeholder={side.target}
            className="input-surface h-8 w-20 rounded-md border px-2 font-mono text-foreground" />
        </label>
        <Button size="xsm" variant="outline" isLoading={busy} onClick={mark}>Mark {direction} here</Button>
      </div>
      {lastPrice != null ? <p className="px-1 pt-1 text-[11px] text-muted-text">Last {price(lastPrice)}: the stop goes {side.stop} it, the target {side.target}.</p> : null}
      <div className="mt-2 flex flex-wrap items-center gap-2 px-1 text-xs">
        {draft ? <>
          <Button size="xsm" variant="outline" isLoading={busy} onClick={keep}>Keep box {price(draft.bottom)}–{price(draft.top)}</Button>
          <Button size="xsm" variant="ghost" onClick={clearDraft}>Discard</Button>
        </> : <Button size="xsm" variant="ghost" onClick={draw} disabled={drawing}><Square className="h-3.5 w-3.5" />{drawing ? 'Click two corners on the chart' : 'Draw a box'}</Button>}
      </div>
      {error ? <p role="alert" className="px-1 pt-1 text-xs text-danger">{error}</p> : null}
      {yours.length ? <ul className="mt-2 space-y-1.5 text-xs">{yours.map((item) => (
        <li key={item.id} className="flex items-start justify-between gap-2 rounded-lg border border-border/40 px-2 py-1.5">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5"><Badge variant={item.status === 'open' ? 'warning' : 'default'}>{STATUS[item.status] || item.status}</Badge>
              <span className="text-secondary-text">{describe(item)}</span></div>
            {item.r != null ? <p className="mt-0.5 text-muted-text">{item.status === 'closed' ? `${item.how} · ` : 'now '}<strong className="text-foreground">{signedR(item.r)}</strong>
              {item.twinR != null ? ` · random timing ${signedR(item.twinR)}` : item.status === 'closed' ? ' · random timing after its five sessions' : ''}</p> : null}
          </div>
          {item.status === 'waiting' ? <button type="button" aria-label="Remove" className="shrink-0 text-muted-text hover:text-danger"
            onClick={() => void run(() => tradeDeskApi.deleteChartYours(item.id))}><Trash2 className="h-3.5 w-3.5" /></button> : null}
        </li>))}</ul> : null}
    </Card>
  );
}

function verdict(row: ChartGradeKind) {
  if (row.verdict === 'passes') return `Passes: beats random timing (t ${row.t?.toFixed(2)} ≥ ${row.tNeeded}) and made money`;
  if (row.verdict === 'no_edge') return `No edge shown (t ${row.t == null ? '—' : row.t.toFixed(2)}${row.tNeeded ? `, needs ${row.tNeeded}` : ''})`;
  return `Too early: ${row.graded} of ${row.needed} graded`;
}

/** One row per source: the system's flags by kind, your marks and your boxes. */
export function GradesCard({ version }: { version: number }) {
  const [table, setTable] = useState<ChartGradeTable | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    tradeDeskApi.getChartGrades().then((data) => { setTable(data); setError(''); }).catch((failure) => setError(errorMessage(failure)));
  }, [version]);
  if (!table && !error) return null;
  return (
    <Card variant="bordered" padding="sm" data-testid="chart-grades">
      <h3 className="px-1 pb-1 text-sm font-semibold text-foreground">Forward grades</h3>
      <p className="px-1 pb-2 text-[11px] text-muted-text">A system flag counts only if the chart showed it before it traded. Each trade follows the backtest's exits and is compared with the same trade at random times; a kind gets a verdict after {table?.minTrades ?? 30} graded trades.</p>
      {error ? <p className="px-1 text-xs text-secondary-text">{error}</p> : null}
      {table ? <div className="overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs">
        <thead className="text-muted-text"><tr><th className="px-2 py-1 font-normal">Source</th><th className="px-2 py-1 text-right font-normal">Graded</th>
          <th className="px-2 py-1 text-right font-normal">Mean R</th><th className="px-2 py-1 text-right font-normal">Random timing</th>
          <th className="px-2 py-1 text-right font-normal">Edge</th><th className="px-2 py-1 font-normal">Verdict</th><th className="px-2 py-1 font-normal">Waiting · open · expired</th></tr></thead>
        <tbody>{table.kinds.map((row) => <tr key={`${row.source}-${row.kind}`} className="border-t border-border/30">
          <td className="px-2 py-1 text-foreground">{row.label}</td>
          <td className="px-2 py-1 text-right font-mono">{row.graded}/{row.needed}</td>
          <td className="px-2 py-1 text-right font-mono">{signedR(row.meanR)}</td>
          <td className="px-2 py-1 text-right font-mono">{signedR(row.twinR)}</td>
          <td className="px-2 py-1 text-right font-mono">{signedR(row.edge)}</td>
          <td className="px-2 py-1 text-secondary-text">{verdict(row)}</td>
          <td className="px-2 py-1 font-mono text-muted-text">{row.waiting} · {row.open} · {row.expired}{row.late ? ` · ${row.late} late` : ''}{row.void ? ` · ${row.void} void` : ''}</td>
        </tr>)}</tbody>
      </table></div> : null}
    </Card>
  );
}
