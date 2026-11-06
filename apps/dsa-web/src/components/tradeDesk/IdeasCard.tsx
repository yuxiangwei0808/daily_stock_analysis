import { useEffect, useState } from 'react';
import { LineChart } from 'lucide-react';
import { tradeDeskApi, tradeDeskTicker } from '../../api/tradeDesk';
import { Card } from '../common';
import type { RecentIdea, RecentIdeas } from '../../types/tradeDesk';

const SOURCE: Record<string, string> = { high: 'Review: high', medium: 'Review: medium', rejected: 'Rejected by the review', breakout: 'Breakout alert' };
const level = (value?: number | null) => (value == null ? '—' : value.toFixed(2));
const signed = (value?: number | null) => (value == null ? '—' : `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(1)}%`);

function result(item: RecentIdea) {
  if (item.status === 'closed') return `${signed(item.returnPct)}${item.reason ? ` · ${item.reason}` : ''}`;
  return item.returnPct != null ? `running ${signed(item.returnPct)}` : 'running';
}

/** The trend scan's ideas and the breakout alerts as tracked forward, each labelled with its rules' verdicts.

They stay on the dashboard: Discord gets an idea only once every rule it rests on has passed (methodology stage 8). */
export function IdeasCard({ onChart }: { onChart: (ticker: string) => void }) {
  const [data, setData] = useState<RecentIdeas | null>(null);
  const [rejected, setRejected] = useState(false);
  useEffect(() => {
    const load = async () => {
      try { setData(await tradeDeskApi.getRecentIdeas()); } catch { setData(null); }  // the tracker is optional
    };
    void load();
  }, []);
  if (!data) return null;
  const items = data.items.filter((item) => rejected || item.verdict !== 'rejected');
  const rules = [...data.rules.idea, ...data.rules.breakout.filter((rule) => !data.rules.idea.some((other) => other.id === rule.id))];
  const held = !data.discord.idea || !data.discord.breakout;
  return (
    <Card variant="bordered" padding="sm" data-testid="recent-ideas">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-1 pb-1">
        <h3 className="text-sm font-semibold text-foreground">Trade ideas, last {data.days} days</h3>
        <label className="flex items-center gap-1.5 text-xs text-secondary-text">
          <input type="checkbox" checked={rejected} onChange={(event) => setRejected(event.target.checked)} />Show the ones the review rejected
        </label>
      </div>
      <ul className="px-1 pb-2 text-[11px] text-muted-text">{rules.map((rule) => <li key={rule.id}>{rule.label}</li>)}</ul>
      {held ? <p className="px-1 pb-2 text-[11px] text-muted-text">Shown here and tracked forward, not sent to Discord: an idea goes there only once every rule it rests on has passed (trading methodology, stage 8).</p> : null}
      {items.length ? <div className="overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs">
        <thead className="text-muted-text"><tr><th className="px-1 py-1 font-normal">Day</th><th className="px-1 py-1 font-normal">Stock</th>
          <th className="px-1 py-1 font-normal">Side</th><th className="px-1 py-1 font-normal">Source</th><th className="px-1 py-1 text-right font-normal">Entry</th>
          <th className="px-1 py-1 text-right font-normal">Stop</th><th className="px-1 py-1 text-right font-normal">Target</th>
          <th className="px-1 py-1 font-normal">Result</th><th className="px-1 py-1" /></tr></thead>
        <tbody>{items.map((item) => {
          const ticker = tradeDeskTicker(item.ticker);
          return <tr key={item.id} className="border-t border-border/30">
            <td className="px-1 py-1 font-mono text-secondary-text">{item.signalDay}</td>
            <td className="px-1 py-1 text-foreground">{item.ticker}</td>
            <td className="px-1 py-1">{item.direction === 'long' ? 'Long' : 'Short'}</td>
            <td className="px-1 py-1 text-secondary-text">{SOURCE[item.verdict] || item.verdict}</td>
            <td className="px-1 py-1 text-right font-mono">{level(item.entry)}</td>
            <td className="px-1 py-1 text-right font-mono">{level(item.stop)}</td>
            <td className="px-1 py-1 text-right font-mono">{level(item.target)}</td>
            <td className="px-1 py-1 font-mono text-secondary-text">{result(item)}</td>
            <td className="px-1 py-1 text-right">{ticker ? <button type="button" aria-label={`Chart ${ticker}`} onClick={() => onChart(ticker)}
              className="text-muted-text hover:text-cyan"><LineChart className="h-3.5 w-3.5" /></button> : null}</td>
          </tr>;
        })}</tbody>
      </table></div> : <p className="px-1 text-xs text-secondary-text">No ideas in the last {data.days} days.</p>}
    </Card>
  );
}
