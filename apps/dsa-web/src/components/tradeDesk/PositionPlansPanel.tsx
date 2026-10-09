import { Fragment, useCallback, useEffect, useState } from 'react';
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';
import { Link } from 'react-router-dom';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Badge, Button } from '../common';
import type { PlanStatus, PositionAction, PositionPlanRow, PositionPlansResponse } from '../../types/tradeDesk';

const POLL_MS = 30_000;
const REVIEW_POLL_MS = 5_000;
const COLLAPSE_KEY = 'dsa.positionPlans.collapsed';

const ACTION: Record<PositionAction, { label: string; variant: 'info' | 'success' | 'warning' | 'danger' | 'default' }> = {
  hold: { label: 'Hold', variant: 'info' },
  add: { label: 'Add', variant: 'success' },
  trim: { label: 'Trim', variant: 'warning' },
  take_profit: { label: 'Take profit', variant: 'success' },
  close: { label: 'Close', variant: 'danger' },
  roll: { label: 'Roll', variant: 'warning' },
  hedge: { label: 'Hedge', variant: 'warning' },
  review: { label: 'Review', variant: 'default' },
};

const STATUS: Record<PlanStatus, { label: string; row: string; pill: string } | null> = {
  stop_hit: { label: 'Stop hit', row: 'bg-danger/10', pill: 'bg-danger/15 text-danger' },
  near_stop: { label: 'Near stop', row: 'bg-danger/5', pill: 'bg-danger/10 text-danger' },
  target_hit: { label: 'Target reached', row: 'bg-success/10', pill: 'bg-success/15 text-success' },
  near_target: { label: 'Near target', row: 'bg-success/5', pill: 'bg-success/10 text-success' },
  ok: null,
};

function readCollapsed(): boolean {
  try { return window.localStorage.getItem(COLLAPSE_KEY) === '1'; } catch { return false; }
}

function writeCollapsed(value: boolean) {
  try { window.localStorage.setItem(COLLAPSE_KEY, value ? '1' : '0'); } catch { /* a per-viewer convenience only */ }
}

const num = (value?: number | null) => (value == null ? '—' : value.toFixed(2));
const signed = (value?: number | null, digits = 1) => (value == null ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(digits)}%`);
const tone = (value?: number | null) => (value == null ? 'text-secondary-text' : value >= 0 ? 'text-success' : 'text-danger');

function sourceLabel(source: string, basis?: string): string {
  if (source === 'you') return 'your alert';
  if (basis?.startsWith('trailing')) return 'trailing';
  if (source === 'review') return 'review';
  if (source === 'rule') return '2 ATR rule';
  return '';
}

function when(value?: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

/** A level and how far the price is from it. */
function Level({ value, distance, source, basis, near, hit, kind }: {
  value?: number | null; distance?: number | null; source: string; basis?: string; near: boolean; hit: boolean; kind: 'stop' | 'target';
}) {
  if (value == null) return <span className="text-secondary-text">—</span>;
  const color = kind === 'stop' ? 'text-danger' : 'text-success';
  const label = sourceLabel(source, basis);
  return (
    <div>
      <div className={`font-mono text-base font-semibold ${hit || near ? color : 'text-foreground'}`}>{num(value)}</div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-secondary-text">
        {hit ? <span className={`whitespace-nowrap ${color}`}>{kind === 'stop' ? 'hit' : 'reached'}</span>
          : distance != null ? <span className={`whitespace-nowrap ${near ? color : ''}`}>{Math.abs(distance).toFixed(1)}% away</span> : null}
        {label ? <span className="whitespace-nowrap rounded bg-elevated px-1.5 py-0.5 text-[11px] text-secondary-text">{label}</span> : null}
      </div>
    </div>
  );
}

function Details({ row }: { row: PositionPlanRow }) {
  const lines = [
    row.reason ? ['Why', row.reason] : null,
    row.stop != null && row.stopBasis ? ['Stop', `${row.stopBasis}${row.stopNote ? ` (${row.stopNote})` : ''}`] : null,
    row.target != null && row.targetBasis ? ['Target', row.targetBasis] : null,
    row.pnlStopPct != null || row.pnlTargetPct != null
      ? ['P&L levels', [row.pnlStopPct != null ? `cut at ${signed(row.pnlStopPct, 0)}` : '', row.pnlTargetPct != null ? `take profit at ${signed(row.pnlTargetPct, 0)}` : ''].filter(Boolean).join(' · ')]
      : null,
    row.risk ? ['Risk', row.risk] : null,
    row.reviewedAt ? ['Reviewed', when(row.reviewedAt)] : null,
  ].filter((line): line is string[] => Boolean(line));
  if (!lines.length) return <p className="text-sm text-secondary-text">Not reviewed yet: the levels come from the 2 ATR rule until the daily review.</p>;
  return (
    <dl className="grid gap-x-4 gap-y-1.5 text-sm sm:grid-cols-[6.5rem_minmax(0,1fr)]">
      {lines.map(([label, text]) => <Fragment key={label}><dt className="text-secondary-text">{label}</dt><dd className="text-foreground">{text}</dd></Fragment>)}
    </dl>
  );
}

function ActionBadge({ action }: { action: PositionPlanRow['action'] }) {
  if (!action) return <span className="text-sm text-secondary-text">—</span>;
  const item = ACTION[action] ?? ACTION.review;
  return <Badge variant={item.variant}>{item.label}</Badge>;
}

function StatusPill({ status }: { status: PlanStatus }) {
  const item = STATUS[status];
  return item ? <span className={`whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${item.pill}`}>{item.label}</span> : null;
}

/** Your positions with the desk's action, stop and target for each; stops trail and prices are live. */
export function PositionPlansPanel({ showHoldingsLink = true }: { showHoldingsLink?: boolean }) {
  const [data, setData] = useState<PositionPlansResponse | null>(null);
  const [problem, setProblem] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [starting, setStarting] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await tradeDeskApi.getPositionPlans());
      setProblem('');
    } catch {
      setProblem('Plans are unavailable right now.');
    }
  }, []);
  const reviewing = data?.review?.status === 'running';
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, reviewing ? REVIEW_POLL_MS : POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, reviewing]);

  if (!data?.enabled || (!data.items.length && !problem)) return null;
  const review = data.review ?? {};
  const urgent = data.items.filter((row) => row.status !== 'ok').length;
  const startReview = async () => {
    setStarting(true);
    try {
      await tradeDeskApi.reviewPositions();
      await load();
    } catch {
      setProblem('The review could not start (one may already be running).');
    } finally {
      setStarting(false);
    }
  };
  const toggle = () => setCollapsed((value) => { writeCollapsed(!value); return !value; });

  return (
    <section className="rounded-2xl border border-border/60 bg-card/50 p-4 sm:p-5" data-testid="position-plans">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <button type="button" onClick={toggle} aria-expanded={!collapsed} className="flex min-w-0 items-start gap-2 text-left">
          {collapsed ? <ChevronRight className="mt-0.5 h-5 w-5 shrink-0 text-secondary-text" /> : <ChevronDown className="mt-0.5 h-5 w-5 shrink-0 text-secondary-text" />}
          <span>
            <span className="block text-base font-semibold text-foreground sm:text-lg">
              Your positions: plan
              {urgent ? <span className="ml-2 whitespace-nowrap rounded-full bg-danger/15 px-2 py-0.5 align-middle text-xs font-medium text-danger">{urgent} need attention</span> : null}
            </span>
            <span className="mt-0.5 block text-sm text-secondary-text">Action, stop and target for each holding · prices live</span>
          </span>
        </button>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-secondary-text">
            {reviewing ? 'Reviewing your positions… (about a minute)'
              : review.status === 'failed' ? <span className="text-danger">Review failed: {review.error}</span>
                : review.at ? `Reviewed ${when(review.at)}${review.model ? ` · ${review.model}` : ''}` : 'Not reviewed yet (daily after 5 PM ET)'}
          </span>
          <Button size="sm" variant="ghost" onClick={() => void startReview()} isLoading={starting} disabled={reviewing}>
            <RefreshCw className="h-4 w-4" />Review now
          </Button>
          {showHoldingsLink ? <Link to="/trade-desk?view=holdings" className="text-cyan hover:underline">Holdings →</Link> : null}
        </div>
      </div>
      {problem ? <p className="mt-3 text-sm text-danger">{problem}</p> : null}
      {!collapsed ? (
        <>
          {/* Wide screens: a table. */}
          <div className="mt-4 hidden overflow-x-auto md:block">
            <table className="w-full min-w-[720px] text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-muted-text">
                <tr className="border-b border-border/50">
                  <th className="py-2 pr-3 font-medium">Position</th>
                  <th className="py-2 pr-3 text-right font-medium">Price</th>
                  <th className="py-2 pr-3 text-right font-medium">P&amp;L</th>
                  <th className="py-2 pr-3 font-medium">Action</th>
                  <th className="min-w-[9.5rem] py-2 pr-3 font-medium">Stop</th>
                  <th className="min-w-[9.5rem] py-2 font-medium">Target</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((row) => {
                  const expanded = open === row.key;
                  return (
                    <Fragment key={row.key}>
                      <tr className={`cursor-pointer border-b border-border/30 align-top transition hover:bg-hover/50 ${STATUS[row.status]?.row ?? ''}`}
                        onClick={() => setOpen(expanded ? null : row.key)} aria-expanded={expanded} data-testid={`plan-row-${row.key}`}>
                        <td className="py-3 pr-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-mono text-base font-semibold text-foreground">{row.ticker}</span>
                            <StatusPill status={row.status} />
                          </div>
                          <div className="mt-0.5 text-xs text-secondary-text">
                            {row.type === 'option' ? `${row.label.replace(`${row.ticker} `, '')} · levels on ${row.ticker}` : 'shares'}
                            {row.weightPct != null ? ` · ${row.weightPct.toFixed(1)}% of account` : ''}
                            {row.type === 'option' && row.daysLeft != null ? ` · ${row.daysLeft}d left` : ''}
                          </div>
                        </td>
                        <td className="py-3 pr-3 text-right">
                          <div className="font-mono text-base text-foreground">{num(row.price)}</div>
                          <div className={`whitespace-nowrap text-xs ${tone(row.dayPct)}`}>{signed(row.dayPct)} today</div>
                        </td>
                        <td className={`py-3 pr-3 text-right font-mono text-base ${tone(row.pnlPct)}`}>{signed(row.pnlPct)}</td>
                        <td className="py-3 pr-3"><ActionBadge action={row.action} /></td>
                        <td className="py-3 pr-3"><Level kind="stop" value={row.stop} distance={row.stopDistancePct} source={row.stopSource} basis={row.stopBasis}
                          near={row.status === 'near_stop'} hit={row.status === 'stop_hit'} /></td>
                        <td className="py-3"><Level kind="target" value={row.target} distance={row.targetDistancePct} source={row.targetSource} basis={row.targetBasis}
                          near={row.status === 'near_target'} hit={row.status === 'target_hit'} /></td>
                      </tr>
                      {expanded ? <tr className="border-b border-border/30"><td colSpan={6} className="bg-elevated/30 px-3 py-3"><Details row={row} /></td></tr> : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
          {/* Phones: one card per position. */}
          <ul className="mt-4 space-y-2 md:hidden">
            {data.items.map((row) => {
              const expanded = open === row.key;
              return (
                <li key={row.key} className={`rounded-xl border border-border/50 p-3 ${STATUS[row.status]?.row ?? ''}`}>
                  <button type="button" className="w-full text-left text-sm" onClick={() => setOpen(expanded ? null : row.key)} aria-expanded={expanded}>
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-base font-semibold text-foreground">{row.ticker}</span>
                      <ActionBadge action={row.action} />
                      <StatusPill status={row.status} />
                      <span className={`ml-auto font-mono ${tone(row.pnlPct)}`}>{signed(row.pnlPct)}</span>
                    </span>
                    <span className="mt-0.5 block text-xs text-secondary-text">
                      {row.type === 'option' ? row.label : 'shares'} · now {num(row.price)}
                    </span>
                    <span className="mt-2 grid grid-cols-2 gap-2">
                      <span><span className="block text-xs text-muted-text">Stop</span><Level kind="stop" value={row.stop} distance={row.stopDistancePct} source={row.stopSource}
                        basis={row.stopBasis} near={row.status === 'near_stop'} hit={row.status === 'stop_hit'} /></span>
                      <span><span className="block text-xs text-muted-text">Target</span><Level kind="target" value={row.target} distance={row.targetDistancePct} source={row.targetSource}
                        basis={row.targetBasis} near={row.status === 'near_target'} hit={row.status === 'target_hit'} /></span>
                    </span>
                  </button>
                  {expanded ? <div className="mt-3 border-t border-border/40 pt-3"><Details row={row} /></div> : null}
                </li>
              );
            })}
          </ul>
          {review.summary ? (
            <details className="mt-4 rounded-xl border border-border/40 px-3 py-2 text-sm">
              <summary className="cursor-pointer select-none text-secondary-text">The review's summary</summary>
              <p className="mt-2 whitespace-pre-line leading-6 text-foreground">{review.summary}</p>
            </details>
          ) : null}
          <p className="mt-3 text-xs leading-5 text-muted-text">
            Levels come from your alerts first, then the daily review, then a 2 ATR rule. Stops trail: they only move toward the price.
            Crossing a stop or target sends one Discord card. Rules of thumb and the model's judgment, not tested signals; nothing is traded.
          </p>
        </>
      ) : null}
    </section>
  );
}
