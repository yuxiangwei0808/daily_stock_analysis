import { useEffect, useState } from 'react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Card } from '../common';
import type { ShadowDelay, ShadowExitRecent, ShadowExitRow, ShadowExits, ShadowFeedDelays } from '../../types/tradeDesk';

const pct = (value?: number | null) => (value == null ? '—' : `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(1)}%`);
const tone = (value?: number | null) => (value == null ? undefined : value > 0 ? 'hsl(var(--color-success))' : value < 0 ? 'hsl(var(--color-danger))' : undefined);
const REFRESH_MS = 15000;
const seconds = (delay?: ShadowDelay | null) => (delay ? `${delay.median < 10 ? delay.median.toFixed(2) : delay.median.toFixed(0)} s` : null);
const WAITING: Record<string, string> = {
  stale_quote: 'no fresh quote', wide_quote: 'quote too wide', no_bid: 'no bid', old_position: 'position data too old',
};

function verdictLine(data: ShadowExits) {
  if (data.verdict === 'too_early') {
    return `Too early to judge: ${data.finished ?? 0} of ${data.needed ?? 30} positions finished, over ${data.months ?? 0} month${data.months === 1 ? '' : 's'} (needs at least 2).`;
  }
  const detail = `${data.finished} positions over ${data.months} months, t ${data.t ?? '—'} (needs ${data.tNeeded ?? '—'})`;
  if (data.verdict === 'selling_better') return `Selling beat holding: ${detail}.`;
  if (data.verdict === 'holding_better') return `Holding beat selling: ${detail}.`;
  return `No clear difference between selling and holding: ${detail}.`;
}

function feedLine(data: ShadowExits) {
  const feed = data.feed;
  if (feed?.state === 'live') return `Live: every quote is checked as it arrives. Last quote ${feed.lastQuoteS ?? '—'} s ago, positions read ${feed.positionsReadS ?? '—'} s ago.`;
  if (feed?.state === 'snapshot') return 'Pushed quotes have stopped: checking on 30-second snapshots until they return.';
  if (feed?.state === 'down') return `No live quotes right now, so nothing is being checked${feed.errors && Object.keys(feed.errors).length ? ` (${Object.values(feed.errors).join(', ')})` : ''}.`;
  if (feed?.state === 'closed') return 'Outside the regular session. It connects a few minutes before the open.';
  return 'The live feed is not running on this server.';
}

function delayLine(delays?: ShadowFeedDelays | null) {
  if (!delays) return null;
  const parts = [
    ['stock quotes', seconds(delays.stockQuoteS)], ['option quotes', seconds(delays.optionQuoteS)],
    ['order books', seconds(delays.bookS)], ['your fill to the position list', seconds(delays.fillToPositionS)],
  ].filter(([, value]) => value).map(([label, value]) => `${label} ${value}`);
  return parts.length ? parts.join(', ') : null;
}

function speedLine(data: ShadowExits) {
  const speed = data.speed;
  if (!speed || !(speed.fromPush || speed.fromSnapshot || speed.gaveUp)) return null;
  const parts = [`${speed.fromPush ?? 0} seen on pushed quotes, ${speed.fromSnapshot ?? 0} on snapshots`];
  if (speed.checkMs) parts.push(`checked ${speed.checkMs.median.toFixed(0)} ms after arriving`);
  if (speed.exitWaitS) parts.push(`orders waited ${seconds(speed.exitWaitS)} (95% within ${speed.exitWaitS.p95.toFixed(0)} s)`);
  if (speed.gaveUp) parts.push(`${speed.gaveUp} gave up with no usable quote`);
  return `Crossings: ${parts.join('; ')} (medians).`;
}

function ResultTable({ title, rows }: { title: string; rows: ShadowExitRow[] }) {
  if (!rows.length) return null;
  return (
    <div className="overflow-x-auto">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-secondary-text">{title}</h3>
      <table className="mt-1 w-full text-xs">
        <thead className="text-muted-text"><tr>
          <th className="py-1 text-left font-normal" />
          <th className="pl-2 text-right font-normal">Positions</th><th className="pl-2 text-right font-normal">Finished</th>
          <th className="pl-2 text-right font-normal">If sold</th><th className="pl-2 text-right font-normal">Held 5</th>
          <th className="pl-2 text-right font-normal">Held 10</th><th className="pl-2 text-right font-normal">What you did</th>
          <th className="pl-2 text-right font-normal">Sold − held</th><th className="pl-2 text-right font-normal">Sold better</th>
        </tr></thead>
        <tbody className="divide-y divide-border/40">
          {rows.map((row) => (
            <tr key={row.label}>
              <td className="py-1 text-foreground">{row.label}</td>
              <td className="pl-2 text-right">{row.positions}</td>
              <td className="pl-2 text-right">{row.finished}</td>
              <td className="pl-2 text-right font-mono" style={{ color: tone(row.ifSoldPct) }}>{pct(row.ifSoldPct)}</td>
              <td className="pl-2 text-right font-mono" style={{ color: tone(row.ifHeld5Pct) }}>{pct(row.ifHeld5Pct)}</td>
              <td className="pl-2 text-right font-mono" style={{ color: tone(row.ifHeld10Pct) }}>{pct(row.ifHeld10Pct)}</td>
              <td className="pl-2 text-right font-mono" style={{ color: tone(row.yoursPct) }}>{pct(row.yoursPct)}</td>
              <td className="pl-2 text-right font-mono" style={{ color: tone(row.soldMinusHeldPct) }}>{pct(row.soldMinusHeldPct)}</td>
              <td className="pl-2 text-right">{row.soldBetterPct == null ? '—' : `${row.soldBetterPct}%`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function recentState(item: ShadowExitRecent) {
  if (item.gaveUp) return 'no order: no usable quote within 5 minutes';
  if (item.ended === 'no_exit') return 'no order: closed or expired first';
  if (item.problems?.length) return `waiting: ${item.problems.map((problem) => WAITING[problem] || problem).join(', ')}`;
  const waited = item.waitS ? ` after ${item.waitS.toFixed(0)} s` : '';
  const sold = item.ifSoldPct == null ? 'no price' : `would sell at ${pct(item.ifSoldPct)} on cost${waited}`;
  return item.finished ? `${sold}, held ${pct(item.ifHeld10Pct)}` : sold;
}

/** Shadow mode: what the system would have sold, compared with holding. Nothing is sent to the broker. */
export function ShadowExitsCard() {
  const [data, setData] = useState<ShadowExits | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const load = () => {
      if (document.hidden) return;
      tradeDeskApi.getShadowExits().then((loaded) => { setData(loaded); setError(''); })
        .catch((loadError) => setError(loadError instanceof Error ? loadError.message : 'Shadow results could not be loaded'));
    };
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, []);
  if (error && !data) return <Card variant="bordered" padding="md"><p className="text-xs text-secondary-text">{error}</p></Card>;
  if (!data?.enabled) return null;
  return (
    <Card variant="bordered" padding="md">
      <div data-testid="shadow-exits" className="space-y-3">
        <div>
          <h2 className="text-lg font-semibold text-foreground">Shadow exits</h2>
          <p className="text-xs text-secondary-text">{data.note}</p>
          <p data-testid="shadow-feed" className="mt-1 text-xs text-foreground">{feedLine(data)}</p>
          {delayLine(data.feed)
            ? <p className="text-xs text-secondary-text">Delay today, median: {delayLine(data.feed)}.</p>
            : data.feedLastDay && delayLine(data.feedLastDay)
              ? <p className="text-xs text-secondary-text">Delay on {data.feedLastDay.day}, median: {delayLine(data.feedLastDay)}.</p>
              : null}
          {error ? <p className="text-xs text-secondary-text">Could not refresh: {error}</p> : null}
        </div>
        {data.recorded ? (
          <>
            <p className="text-sm text-foreground">{verdictLine(data)}</p>
            <p className="text-xs text-secondary-text">
              P&amp;L on cost, each position once. Sold at the bid or ask, before commissions; held is the mid after 5 and 10 sessions or at expiry.
            </p>
            {speedLine(data) ? <p className="text-xs text-secondary-text">{speedLine(data)}</p> : null}
            <ResultTable title="By position" rows={data.rows || []} />
            <ResultTable title="By trigger" rows={data.byTrigger || []} />
            <div>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-secondary-text">Latest</h3>
              <ul className="mt-1 divide-y divide-border/40 text-xs">
                {(data.recent || []).map((item) => (
                  <li key={`${item.label}-${item.kind}-${item.at}`} className="py-1 text-foreground">
                    {item.label} · {item.kindLabel} · {new Date(item.at).toLocaleString()}
                    <span className="text-secondary-text"> · {recentState(item)}{item.skip ? ' · a spread: the first live version would skip it' : ''}</span>
                  </li>
                ))}
              </ul>
            </div>
          </>
        ) : <p className="text-sm text-secondary-text">No would-be exits yet. It checks every live quote during regular hours.</p>}
        <p className="text-xs text-muted-text">
          {data.levelChanges ?? 0} plan level changes saved for later grading{data.since ? ` since ${new Date(data.since).toLocaleDateString()}` : ''}.
          {data.carried ? ` ${data.carried} levels were already crossed when shadow mode started, so they aren't graded.` : ''}
        </p>
      </div>
    </Card>
  );
}
