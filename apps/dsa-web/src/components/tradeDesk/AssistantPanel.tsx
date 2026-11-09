import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { BellRing, ChevronRight, Play, Square } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Badge, Button, Card } from '../common';
import type { AssistantCalls, AssistantContract, AssistantEvent, AssistantHorizon, AssistantTicker, InPlayList } from '../../types/tradeDesk';
import { AssistantPlans } from './AssistantPlans';
import { GLOSSARY, GLOSSARY_INTRO } from './assistantGlossary';
import { nyTime, price } from './chartOverlays';
import { errorMessage } from './deskFormat';
import { ShadowModeProvider } from './shadowMode';
import { useShadowMode } from './shadowModeContext';
import type { ShadowMode } from './shadowModeContext';

const HORIZONS: Array<[AssistantHorizon, string]> = [['0dte', '0DTE options'], ['intraday', 'Intraday'], ['swing', 'Swing']];
const CALLS: Array<[AssistantCalls, string]> = [['selective', 'Selective'], ['active', 'Active']];
const signed = (value?: number | null, digits = 2) => (value == null ? '—' : `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(digits)}%`);
const num = (value?: number | null, digits = 2) => (value == null ? '—' : value.toFixed(digits));
const away = (value: number, from?: number | null) => (from ? signed((value / from - 1) * 100) : '—');

/** A section folded by default, with a one-line summary that stays visible. Uncontrolled, so the live updates
 * keep whatever you opened. */
function Fold({ title, summary, children, testId }: { title: string; summary?: ReactNode; children: ReactNode; testId?: string }) {
  return (
    <details className="group rounded-lg border border-border/40 px-2 py-1.5" data-testid={testId}>
      <summary className="flex cursor-pointer list-none items-baseline gap-1 text-xs [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-3.5 w-3.5 shrink-0 self-center text-muted-text transition-transform group-open:rotate-90" aria-hidden="true" />
        <span className="shrink-0 whitespace-nowrap font-semibold text-foreground">{title}</span>
        {summary ? <span className="min-w-0 text-muted-text">· {summary}</span> : null}
      </summary>
      <div className="pt-2">{children}</div>
    </details>
  );
}

function ContractRows({ rows }: { rows: AssistantContract[] }) {
  // Every row is valued at the same levels: one column a level, named once in the header.
  const levels = rows.flatMap((row) => row.atLevels || []).filter((level, k, all) => all.findIndex((other) => other.id === level.id) === k);
  return (
    <div className="overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs">
      <thead className="text-muted-text"><tr>
        <th className="px-1 py-1 font-normal">Strike</th><th className="px-1 py-1 text-right font-normal">Bid / ask</th>
        <th className="px-1 py-1 text-right font-normal">Spread</th><th className="px-1 py-1 text-right font-normal">IV</th>
        <th className="px-1 py-1 text-right font-normal">Δ</th><th className="px-1 py-1 text-right font-normal">To break even</th>
        <th className="px-1 py-1 text-right font-normal">Decay a minute</th>
        {levels.map((level) => <th key={level.id} className="px-1 py-1 text-right font-normal">At {level.label} {price(level.price)}</th>)}
      </tr></thead>
      <tbody>{rows.map((row) => <tr key={row.code} className="border-t border-border/30">
        <td className="px-1 py-1 font-mono text-foreground">{price(row.strike)} {row.right === 'call' ? 'C' : 'P'}</td>
        <td className="px-1 py-1 text-right font-mono">{num(row.bid)} / {num(row.ask)}</td>
        <td className="px-1 py-1 text-right font-mono">{row.spreadPct == null ? '—' : `${row.spreadPct.toFixed(1)}%`}</td>
        <td className="px-1 py-1 text-right font-mono">{row.iv == null ? '—' : `${row.iv.toFixed(0)}%`}</td>
        <td className="px-1 py-1 text-right font-mono">{num(row.delta)}</td>
        <td className="px-1 py-1 text-right font-mono">{signed(row.breakevenMovePct)}</td>
        <td className="px-1 py-1 text-right font-mono">{row.thetaPerMin == null ? '—' : num(row.thetaPerMin, 3)}</td>
        {levels.map((level) => {
          const at = (row.atLevels || []).find((other) => other.id === level.id);
          return <td key={level.id} className="px-1 py-1 text-right font-mono text-secondary-text">{at ? `${num(at.now)} → ${num(at.inHalfHour)}` : '—'}</td>;
        })}
      </tr>)}</tbody>
    </table>
    {levels.length ? <p className="pt-1 text-[11px] text-muted-text">At a level: the contract's estimated worth if the stock trades there now → in 30 minutes (Black-Scholes at the quoted IV).</p> : null}
    </div>
  );
}

const FLAG_HELP: Record<string, string> = {
  fade: 'A fade is a limit order at a box edge for the first return to the box: fade short sells at the bottom edge when the price comes back up from below; fade long buys at the top edge when it comes back down from above. Stop past the box, target twice the stop distance.',
  break_retest: 'A break and retest waits for a close outside the box, away from the big move, then a limit back at the box edge against that move; stop past the box, target the earlier box.',
};

function BoxFlags({ item }: { item: AssistantTicker }) {
  const flags = [...item.flags].sort((a, b) => Math.abs(a.price - (item.price || 0)) - Math.abs(b.price - (item.price || 0)));
  if (!flags.length) return null;
  const words = (flag: AssistantTicker['flags'][number]) => `${flag.kind === 'break_retest' ? 'break and retest' : flag.kind.replace('_', ' ')} ${flag.direction}`;
  const kinds = Array.from(new Set(flags.map((flag) => flag.kind)));
  return (
    <Fold title="Box flags" testId="assistant-flags"
      summary={`${flags.length}; nearest ${words(flags[0])} at ${price(flags[0].price)} (${away(flags[0].price, item.price)} away). Chart rule, no edge shown`}>
      <ul className="space-y-1 text-xs text-secondary-text">{flags.map((flag) => <li key={`${flag.kind}-${flag.box}-${flag.direction}-${flag.price}`}>
        <Badge variant={flag.state === 'open' ? 'warning' : 'default'}>{flag.state}</Badge> {words(flag)} ({flag.box} box) at {price(flag.price)}
        <span className="text-muted-text"> ({away(flag.price, item.price)} away)</span>, stop {price(flag.stop)}, target {price(flag.target)}
        <span className="text-muted-text"> · {flag.grade}</span></li>)}</ul>
      <p className="pt-1.5 text-[11px] text-muted-text">The chart's box rule: a big 15-minute move, then the boxes the bars around it make.
        {kinds.map((kind) => (FLAG_HELP[kind] ? ` ${FLAG_HELP[kind]}` : '')).join('')} Armed: the next 15-minute bar fills it if it trades there;
        watching: it needs that close first. In the backtest neither trade beat the same trades at random times.</p>
    </Fold>
  );
}

/** The levels, the price's place among them marked; folded to the nearest one each side. */
function Levels({ item }: { item: AssistantTicker }) {
  const above = item.levels.filter((level) => (level.distancePct ?? 0) > 0);
  const below = item.levels.filter((level) => (level.distancePct ?? 0) <= 0);
  const near = (level?: AssistantTicker['levels'][number]) => (level ? `${level.label} ${price(level.price)} (${signed(level.distancePct)})` : 'none');
  const row = (level: AssistantTicker['levels'][number]) => <tr key={level.id} className="border-t border-border/30">
    <td className="px-1 py-0.5 text-foreground">{level.label}</td>
    <td className="px-1 py-0.5 text-right font-mono">{price(level.price)}</td>
    <td className="px-1 py-0.5 text-right font-mono text-secondary-text">{signed(level.distancePct)}</td>
    <td className="px-1 py-0.5 text-muted-text">{level.verdict ?? 'yours'}</td>
  </tr>;
  return (
    <Fold title={`Levels (${item.levels.length})`} testId="assistant-levels"
      summary={!item.levels.length ? 'none yet' : item.price == null ? 'waiting for a price'
        : `above: ${near(above[above.length - 1])}; below: ${near(below[0])}`}>
      <table className="w-full max-w-2xl text-left text-xs"><tbody>
        {above.map(row)}
        {item.price != null ? <tr className="border-t border-cyan/40"><td className="px-1 py-0.5 text-cyan">Price now</td>
          <td className="px-1 py-0.5 text-right font-mono text-cyan">{price(item.price)}</td><td colSpan={2} /></tr> : null}
        {below.map(row)}
      </tbody></table>
      <p className="pt-1 text-[11px] text-muted-text">Distance is the level against the price now. None of these levels has passed a test; yours are your stops and targets.</p>
    </Fold>
  );
}

/** The card's labels explained, folded at the bottom so it never sits between you and the notes. */
function Glossary() {
  return (
    <Fold title="What these mean" testId="assistant-glossary" summary="the card's labels, section by section">
      <p className="text-[11px] text-muted-text">{GLOSSARY_INTRO}</p>
      <div className="grid gap-x-6 gap-y-3 pt-2 lg:grid-cols-2">{GLOSSARY.map((section) => <section key={section.title}>
        <h5 className="pb-0.5 text-[11px] font-semibold text-secondary-text">{section.title}</h5>
        <dl className="space-y-0.5 text-[11px]">{section.entries.map(([term, meaning]) => <div key={term}>
          <dt className="inline font-medium text-secondary-text">{term}</dt><dd className="inline text-muted-text">: {meaning}</dd>
        </div>)}</dl>
      </section>)}</div>
    </Fold>
  );
}

/** The watcher's event notes are in each stock's notes; the alerts keep what sounds and the analyst's notes.
 * The server titles them "TICKER: watcher note" (assistant.py, _watch). */
const isWatcherNote = (event: AssistantEvent) => event.kind === 'note' && event.title.endsWith('watcher note');

function Notes({ item }: { item: AssistantTicker }) {
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const ask = async () => {
    if (!question.trim()) return;
    setBusy(true); setError('');
    try { await tradeDeskApi.askAssistant(item.ticker, question); setQuestion(''); } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(false); }
  };
  const notes = item.notes || [];
  const latest = item.watch && item.watch.trigger === 'minute' && !item.watch.escalate ? item.watch : null;  // the rest are in the notes
  return (
    <div data-testid="assistant-notes">
      <h4 className="pb-1 text-xs font-semibold text-foreground">Assistant</h4>
      <form className="flex flex-wrap gap-2 text-xs" onSubmit={(event) => { event.preventDefault(); void ask(); }}>
        <input aria-label="Ask the assistant" value={question} onChange={(event) => setQuestion(event.target.value)}
          placeholder={`Ask about ${item.ticker}: "take profit here?", "where is the risk?"`}
          className="input-surface h-8 min-w-[16rem] flex-1 rounded-md border px-2 text-foreground" />
        <Button type="submit" size="xsm" variant="outline" isLoading={busy}>Ask</Button>
      </form>
      {error ? <p role="alert" className="pt-1 text-xs text-danger">{error}</p> : null}
      {latest ? <p className="mt-2 text-xs text-secondary-text" data-testid="watcher-latest">
        <span className="font-mono text-muted-text">{nyTime(latest.at)}</span>{' '}
        <span className="rounded bg-border/40 px-1 text-[10px] text-muted-text">Watcher, each minute</span>{' '}
        <span className={latest.kind === 'held' ? 'text-muted-text' : 'text-foreground'}>{latest.text}</span>
        {latest.why ? <span className="text-muted-text"> · {latest.why}</span> : null}
      </p> : null}
      {notes.length ? <ul className="mt-2 max-h-48 space-y-1.5 overflow-y-auto text-xs">{notes.map((note) => <li key={`${note.at}-${note.kind}`}
        className={note.kind === 'held' ? 'text-muted-text' : 'text-secondary-text'}>
        <span className="font-mono text-muted-text">{nyTime(note.at)}</span>{' '}
        {note.tier ? <span className={`rounded px-1 text-[10px] ${note.tier === 'analyst' ? 'bg-cyan/10 text-cyan' : 'bg-border/40 text-muted-text'}`}>{note.tier === 'analyst' ? 'Analyst' : 'Watcher'}</span> : null}{note.tier ? ' ' : null}
        {note.question ? <span className="text-foreground">You: {note.question} · </span> : null}
        <span className={note.kind === 'held' ? '' : 'text-foreground'}>{note.text}</span>
        {note.tier === 'watcher' && note.why && !note.escalate ? <span className="text-muted-text"> · {note.why}</span> : null}
        {note.model ? <span className="text-muted-text"> · {note.model}{note.seconds != null ? `, ${note.seconds.toFixed(0)} s` : ''}</span> : null}
        {note.tier === 'watcher' && note.why && note.escalate ? <span className="block text-cyan">{`→ Analyst: ${note.why}`}</span> : null}
        {note.call ? <span className="block text-warning">Call: {note.call} (untested model call, graded against random timing)</span> : null}
      </li>)}</ul> : <p className="pt-1 text-xs text-muted-text">Notes appear when something changes, at most every few minutes a stock, and right away for a fill, a stop or a target.</p>}
    </div>
  );
}

function TickerDetail({ item }: { item: AssistantTicker }) {
  return (
    <div className="space-y-3">
      <p className="text-sm text-foreground"><span className="font-semibold">{item.ticker}</span> <span className="font-mono">{item.price == null ? '—' : price(item.price)}</span>
        <span className="text-secondary-text"> {signed(item.changePct)}{item.quoteAgeS != null ? ` · last trade ${item.quoteAgeS.toFixed(1)} s ago` : ''}</span>
        {!item.loaded ? <span className="text-muted-text"> · loading levels…</span> : null}{item.error ? <span className="text-danger"> · {item.error}</span> : null}</p>
      <Notes item={item} />
      <div>
        <h4 className="pb-1 text-xs font-semibold text-foreground">Your positions</h4>
        {item.positions.length ? <ul className="space-y-1 text-xs">{item.positions.map((row) => <li key={row.key} className="text-secondary-text">
          <span className="text-foreground">{row.label}</span> · {signed(row.pnlPct, 1)} on cost · {row.weightPct == null ? '—' : `${row.weightPct.toFixed(1)}%`} of the account
          {row.stop != null ? ` · stop ${price(row.stop)}` : ''}{row.target != null ? ` · target ${price(row.target)}` : ''}
          {row.daysLeft === 0 ? ' · expires today' : ''} <span className="text-muted-text">({row.source})</span></li>)}</ul>
          : <p className="text-xs text-muted-text">None on {item.ticker}. A fill shows here within seconds while the live position feed runs (regular hours).</p>}
      </div>
      <AssistantPlans ticker={item.ticker} plans={item.plans || []} />
      <div className="space-y-2">
        <Levels item={item} />
        <BoxFlags item={item} />
        <Fold title="Contracts" testId="assistant-contracts"
          summary={item.contracts.rows.length
            ? `${item.contracts.rows.length} expiring ${item.contracts.expiry || '—'}, strikes ${price(Math.min(...item.contracts.rows.map((row) => row.strike)))}–${price(Math.max(...item.contracts.rows.map((row) => row.strike)))}`
            : item.contracts.error ? `none: ${item.contracts.error}` : 'loading the chain…'}>
          {item.contracts.rows.length ? <ContractRows rows={item.contracts.rows} />
            : <p className="text-xs text-muted-text">{item.contracts.error ? `No contracts: ${item.contracts.error}` : 'Loading the chain…'}</p>}
        </Fold>
      </div>
    </div>
  );
}

/** The trading assistant: start it on one to three stocks; levels, contracts, your positions and alerts, live. */
/** Shadow mode's controls and feed on the Chart tab; the Trade Desk page shares one stream (shadowMode.tsx). */
export function AssistantPanel(props: { current: string; onShow: (ticker: string) => void }) {
  const shadow = useShadowMode();
  if (shadow) return <ShadowModePanel {...props} shadow={shadow} />;
  return <ShadowModeProvider><StandalonePanel {...props} /></ShadowModeProvider>;
}

function StandalonePanel(props: { current: string; onShow: (ticker: string) => void }) {
  const shadow = useShadowMode();
  return shadow ? <ShadowModePanel {...props} shadow={shadow} /> : null;
}

function ShadowModePanel({ current, onShow, shadow }: { current: string; onShow: (ticker: string) => void; shadow: ShadowMode }) {
  const { state, setState, permission } = shadow;
  const [names, setNames] = useState(current);
  const [horizon, setHorizon] = useState<AssistantHorizon>('0dte');
  const [calls, setCalls] = useState<AssistantCalls>('selective');
  const [focus, setFocus] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [inPlay, setInPlay] = useState<InPlayList | null>(null);

  useEffect(() => {
    void (async () => { try { setInPlay(await tradeDeskApi.getInPlay()); } catch { setInPlay(null); } })();
  }, []);
  useEffect(() => { if (current) setNames((value) => value || current); }, [current]);

  const start = async (chosen?: string[]) => {
    const tickers = chosen || names.split(/[\s,]+/).map((name) => name.trim().toUpperCase()).filter(Boolean);
    if (!tickers.length) { setError('Enter one to three stocks'); return; }
    shadow.prime();  // created on the click, so the alerts may play
    setBusy(true); setError('');
    try {
      const next = await tradeDeskApi.startAssistant(tickers, horizon, calls);
      shadow.markSeen(next.seq);
      setState(next); setFocus(next.tickers[0]?.ticker || '');
      if (next.tickers[0]) onShow(next.tickers[0].ticker);
    } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(false); }
  };
  const stop = async () => {
    setBusy(true); setError('');
    try { setState(await tradeDeskApi.stopAssistant()); } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(false); }
  };

  if (!state) return null;
  const running = state.running;
  const shown = state.tickers.find((item) => item.ticker === focus) || state.tickers[0];
  const alerts = state.events.filter((event) => !isWatcherNote(event));
  return (
    <Card variant="bordered" padding="md" data-testid="assistant-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-foreground">Shadow mode{running ? `: ${state.tickers.map((item) => item.ticker).join(', ')} · ${HORIZONS.find(([key]) => key === (state.horizon || '0dte'))?.[1]}${state.calls === 'active' ? ' · active calls' : ''}${state.startedAt ? ` · since ${nyTime(state.startedAt)} ET` : ''}` : ' (off)'}</h3>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {permission === 'default' ? <Button size="xsm" variant="ghost" onClick={() => void shadow.allow()}><BellRing className="h-3.5 w-3.5" />Allow notifications</Button> : null}
          {permission === 'denied' ? <span className="text-muted-text">Notifications are blocked in this browser; alerts still sound here.</span> : null}
          {running ? <Button size="xsm" variant="outline" isLoading={busy} onClick={() => void stop()}><Square className="h-3.5 w-3.5" />Turn off shadow mode</Button> : null}
        </div>
      </div>
      {!running ? <form className="mt-2 flex flex-wrap items-end gap-2 text-xs" onSubmit={(event) => { event.preventDefault(); void start(); }}>
        <label className="flex flex-col gap-1 text-secondary-text">Stocks (up to {state.maxTickers})
          <input aria-label="Assistant stocks" value={names} onChange={(event) => setNames(event.target.value.toUpperCase())} placeholder="SPY, QQQ"
            className="input-surface h-8 w-48 rounded-md border px-2 font-mono text-foreground" />
        </label>
        <label className="flex flex-col gap-1 text-secondary-text">Horizon
          <select aria-label="Assistant horizon" value={horizon} onChange={(event) => setHorizon(event.target.value as AssistantHorizon)}
            className="input-surface h-8 rounded-md border px-2 text-foreground">
            {HORIZONS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-secondary-text">Calls
          <select aria-label="Assistant calls" value={calls} onChange={(event) => setCalls(event.target.value as AssistantCalls)}
            className="input-surface h-8 rounded-md border px-2 text-foreground">
            {CALLS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select>
        </label>
        <Button type="submit" size="xsm" variant="outline" isLoading={busy}><Play className="h-3.5 w-3.5" />Turn on shadow mode</Button>
        <p className="basis-full text-[11px] text-muted-text">Calls: <strong className="font-medium">Selective</strong> makes a call only with a clear reason. <strong className="font-medium">Active</strong> also says hold, add, trim or exit for each position you hold whenever something changes, and reads each P&amp;L alert on a position at once. Either way every call is an untested model call, graded after the close.</p>
      </form> : null}
      {error ? <p role="alert" className="pt-1 text-xs text-danger">{error}</p> : null}
      {!running && inPlay?.rows.length ? <div className="mt-2 text-xs" data-testid="in-play">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-secondary-text">In play {inPlay.day} (09:45):</span>
          {inPlay.rows.slice(0, inPlay.top).map((row) => <button key={row.ticker} type="button" onClick={() => setNames((value) =>
            Array.from(new Set([...value.split(/[\s,]+/).filter(Boolean), row.ticker])).slice(-state.maxTickers).join(', '))}
            className="rounded-md border border-border/50 px-2 py-0.5 font-mono text-secondary-text hover:text-foreground">
            {row.ticker} <span className="text-muted-text">{row.s.toFixed(1)}×</span></button>)}
          <Button size="xsm" variant="ghost" isLoading={busy} onClick={() => void start(inPlay.rows.slice(0, state.maxTickers).map((row) => row.ticker))}>
            <Play className="h-3.5 w-3.5" />Shadow the top {Math.min(state.maxTickers, inPlay.rows.length)}</Button>
        </div>
        <p className="pt-1 text-[11px] text-muted-text">{inPlay.label}. It says which stocks will move more today, not which way.</p>
      </div> : null}
      {!running ? <p className="pt-2 text-[11px] text-muted-text">Shadow mode is an add-on to normal mode, for when you watch this page: nothing runs until you turn it on. It watches the stocks until you turn it off or 15 minutes after the close: the day's levels, the option contracts around the price, your positions on them, notes from the model, and an alert with a sound on this page when something changes. Close the page and you simply don't see them; your Discord alerts work the same either way.{state.stopped ? ` Last session: ${state.stopped.reason} at ${nyTime(state.stopped.at)} ET.` : ''}</p> : null}
      {running ? <>
        <div className="mt-3 flex flex-wrap gap-1.5 text-xs" role="group" aria-label="Assistant stocks shown">
          {state.tickers.map((item) => <button key={item.ticker} type="button" aria-pressed={item.ticker === shown?.ticker}
            onClick={() => { setFocus(item.ticker); onShow(item.ticker); }}
            className={`rounded-md border px-2 py-0.5 font-mono ${item.ticker === shown?.ticker ? 'border-cyan/50 text-cyan' : 'border-border/50 text-secondary-text'}`}>
            {item.ticker} {item.price == null ? '' : price(item.price)}</button>)}
        </div>
        {shown ? <div className="mt-3"><TickerDetail item={shown} /></div> : null}
      </> : null}
      {alerts.length ? <div className="mt-3">
        <h4 className="pb-1 text-xs font-semibold text-foreground">Alerts</h4>
        <ul className="max-h-56 space-y-1 overflow-y-auto text-xs">{alerts.slice(0, 30).map((event) => <li key={event.seq} className="text-secondary-text">
          <span className="font-mono text-muted-text">{nyTime(event.at)}</span> <span className="text-foreground">{event.title}</span>{event.detail ? ` · ${event.detail}` : ''}</li>)}</ul>
        <p className="pt-1 text-[11px] text-muted-text">The watcher's notes are under each stock, above.</p>
      </div> : null}
      {state.model && running ? <div className="mt-3"><Fold title="Models today" testId="assistant-models"
        summary={`analyst ${state.model.callsToday} of ${state.model.cap} calls${state.model.watch ? `; watcher ${state.model.watch.callsToday} reads, ${state.model.watch.escalatedToday} sent to the analyst` : ''}${state.model.failed ? `; ${state.model.failed} failed` : ''}`}>
      {state.model ? <p className="text-[11px] text-muted-text">Model today: {state.model.callsToday} of {state.model.cap} calls
        {state.model.by.length ? ` (${state.model.by.map((use) => `${use.label} ${use.calls}`).join(', ')})` : ''}
        {state.model.medianSeconds != null ? `, median ${state.model.medianSeconds.toFixed(0)} s` : ''}{state.model.failed ? `, ${state.model.failed} failed` : ''}.
        {state.model.order ? ` In order: ${state.model.order}.` : ' Your Claude Code plan first, then Codex, the API last.'}</p> : null}
      {state.model.watch ? <p className="pt-1 text-[11px] text-muted-text" data-testid="watcher-line">Watcher today: {state.model.watch.callsToday} reads{state.model.watch.everyMinute ? ` (each stock every completed minute, events within ${state.model.watch.eventSeconds ?? 10} s${state.model.watch.cap ? `; at most ${state.model.watch.cap} a day` : ''})` : ''}, {state.model.watch.escalatedToday} sent to the analyst (at most {state.model.watch.analystPerHour} an hour; your plans, fills, expiries and questions always go). In order: {state.model.watch.order}.
        {state.model.watch.grade ? ` Its escalations, graded by the next ${state.model.watch.grade.forwardMinutes} minutes' range: ${state.model.watch.grade.verdict}${state.model.watch.grade.meanDifferencePct != null ? ` (${state.model.watch.grade.meanDifferencePct > 0 ? '+' : ''}${state.model.watch.grade.meanDifferencePct.toFixed(2)}% vs the stock's other moments${state.model.watch.grade.t != null ? `, t ${state.model.watch.grade.t.toFixed(1)}` : ''})` : ''}; untested.` : ''}</p> : null}
      </Fold></div> : null}
      {running ? <div className="mt-2"><Glossary /></div> : null}
      <p className="pt-2 text-[11px] text-muted-text">{state.note}</p>
    </Card>
  );
}
