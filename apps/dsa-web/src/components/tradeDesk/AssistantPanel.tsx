import { useEffect, useState } from 'react';
import { BellRing, Play, Square } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Badge, Button, Card } from '../common';
import type { AssistantContract, AssistantHorizon, AssistantTicker, InPlayList } from '../../types/tradeDesk';
import { AssistantPlans } from './AssistantPlans';
import { nyTime, price } from './chartOverlays';
import { errorMessage } from './deskFormat';
import { ShadowModeProvider } from './shadowMode';
import { useShadowMode } from './shadowModeContext';
import type { ShadowMode } from './shadowModeContext';

const HORIZONS: Array<[AssistantHorizon, string]> = [['0dte', '0DTE options'], ['intraday', 'Intraday'], ['swing', 'Swing']];
const signed = (value?: number | null, digits = 2) => (value == null ? '—' : `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(digits)}%`);
const num = (value?: number | null, digits = 2) => (value == null ? '—' : value.toFixed(digits));

function ContractRows({ rows }: { rows: AssistantContract[] }) {
  return (
    <div className="overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs">
      <thead className="text-muted-text"><tr>
        <th className="px-1 py-1 font-normal">Strike</th><th className="px-1 py-1 text-right font-normal">Bid / ask</th>
        <th className="px-1 py-1 text-right font-normal">Spread</th><th className="px-1 py-1 text-right font-normal">IV</th>
        <th className="px-1 py-1 text-right font-normal">Δ</th><th className="px-1 py-1 text-right font-normal">To break even</th>
        <th className="px-1 py-1 text-right font-normal">Decay a minute</th><th className="px-1 py-1 font-normal">Worth at a level (now → in 30 min)</th>
      </tr></thead>
      <tbody>{rows.map((row) => <tr key={row.code} className="border-t border-border/30">
        <td className="px-1 py-1 font-mono text-foreground">{price(row.strike)} {row.right === 'call' ? 'C' : 'P'}</td>
        <td className="px-1 py-1 text-right font-mono">{num(row.bid)} / {num(row.ask)}</td>
        <td className="px-1 py-1 text-right font-mono">{row.spreadPct == null ? '—' : `${row.spreadPct.toFixed(1)}%`}</td>
        <td className="px-1 py-1 text-right font-mono">{row.iv == null ? '—' : `${row.iv.toFixed(0)}%`}</td>
        <td className="px-1 py-1 text-right font-mono">{num(row.delta)}</td>
        <td className="px-1 py-1 text-right font-mono">{signed(row.breakevenMovePct)}</td>
        <td className="px-1 py-1 text-right font-mono">{row.thetaPerMin == null ? '—' : num(row.thetaPerMin, 3)}</td>
        <td className="px-1 py-1 text-secondary-text">{(row.atLevels || []).map((level) =>
          <span key={level.id} className="mr-3">{level.label} {price(level.price)}: <span className="font-mono">{num(level.now)} → {num(level.inHalfHour)}</span></span>)}</td>
      </tr>)}</tbody>
    </table></div>
  );
}

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
      {notes.length ? <ul className="mt-2 max-h-48 space-y-1.5 overflow-y-auto text-xs">{notes.map((note) => <li key={`${note.at}-${note.kind}`}
        className={note.kind === 'held' ? 'text-muted-text' : 'text-secondary-text'}>
        <span className="font-mono text-muted-text">{nyTime(note.at)}</span>{' '}
        {note.question ? <span className="text-foreground">You: {note.question} · </span> : null}
        <span className={note.kind === 'held' ? '' : 'text-foreground'}>{note.text}</span>
        {note.call ? <span className="block text-warning">Call: {note.call} (untested model call, graded against random timing)</span> : null}
        {note.model ? <span className="text-muted-text"> · {note.model}{note.seconds != null ? `, ${note.seconds.toFixed(0)} s` : ''}</span> : null}
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
      <div className="grid gap-3 lg:grid-cols-2">
        <div>
          <h4 className="pb-1 text-xs font-semibold text-foreground">Levels</h4>
          <table className="w-full text-left text-xs"><tbody>{item.levels.map((level) => <tr key={level.id} className="border-t border-border/30">
            <td className="px-1 py-0.5 text-foreground">{level.label}</td>
            <td className="px-1 py-0.5 text-right font-mono">{price(level.price)}</td>
            <td className="px-1 py-0.5 text-right font-mono text-secondary-text">{signed(level.distancePct)}</td>
            <td className="px-1 py-0.5 text-muted-text">{level.verdict ?? 'yours'}</td>
          </tr>)}</tbody></table>
        </div>
        <div className="space-y-3">
          <div>
            <h4 className="pb-1 text-xs font-semibold text-foreground">Your positions</h4>
            {item.positions.length ? <ul className="space-y-1 text-xs">{item.positions.map((row) => <li key={row.key} className="text-secondary-text">
              <span className="text-foreground">{row.label}</span> · {signed(row.pnlPct, 1)} on cost · {row.weightPct == null ? '—' : `${row.weightPct.toFixed(1)}%`} of the account
              {row.stop != null ? ` · stop ${price(row.stop)}` : ''}{row.target != null ? ` · target ${price(row.target)}` : ''}
              {row.daysLeft === 0 ? ' · expires today' : ''} <span className="text-muted-text">({row.source})</span></li>)}</ul>
              : <p className="text-xs text-muted-text">None on {item.ticker}. A fill shows here within seconds while the live position feed runs (regular hours).</p>}
          </div>
          {item.flags.length ? <div>
            <h4 className="pb-1 text-xs font-semibold text-foreground">Box flags</h4>
            <ul className="space-y-1 text-xs text-secondary-text">{item.flags.map((flag) => <li key={`${flag.kind}-${flag.box}-${flag.direction}-${flag.price}`}>
              <Badge variant={flag.state === 'open' ? 'warning' : 'default'}>{flag.state}</Badge> {flag.kind.replace('_', ' ')} {flag.direction} at {price(flag.price)},
              stop {price(flag.stop)}, target {price(flag.target)} <span className="text-muted-text">· {flag.grade}</span></li>)}</ul>
          </div> : null}
        </div>
      </div>
      <AssistantPlans ticker={item.ticker} plans={item.plans || []} />
      <div>
        <h4 className="pb-1 text-xs font-semibold text-foreground">Contracts{item.contracts.expiry ? `, expiring ${item.contracts.expiry}` : ''}</h4>
        {item.contracts.rows.length ? <ContractRows rows={item.contracts.rows} />
          : <p className="text-xs text-muted-text">{item.contracts.error ? `No contracts: ${item.contracts.error}` : 'Loading the chain…'}</p>}
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
      const next = await tradeDeskApi.startAssistant(tickers, horizon);
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
  return (
    <Card variant="bordered" padding="md" data-testid="assistant-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-foreground">Shadow mode{running ? `: ${state.tickers.map((item) => item.ticker).join(', ')} · ${HORIZONS.find(([key]) => key === (state.horizon || '0dte'))?.[1]}${state.startedAt ? ` · since ${nyTime(state.startedAt)} ET` : ''}` : ' (off)'}</h3>
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
        <Button type="submit" size="xsm" variant="outline" isLoading={busy}><Play className="h-3.5 w-3.5" />Turn on shadow mode</Button>
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
      {state.events.length ? <div className="mt-3">
        <h4 className="pb-1 text-xs font-semibold text-foreground">Alerts</h4>
        <ul className="max-h-56 space-y-1 overflow-y-auto text-xs">{state.events.slice(0, 30).map((event) => <li key={event.seq} className="text-secondary-text">
          <span className="font-mono text-muted-text">{nyTime(event.at)}</span> <span className="text-foreground">{event.title}</span>{event.detail ? ` · ${event.detail}` : ''}</li>)}</ul>
      </div> : null}
      {state.model && running ? <p className="pt-2 text-[11px] text-muted-text">Model today: {state.model.callsToday} of {state.model.cap} calls
        {state.model.by.length ? ` (${state.model.by.map((use) => `${use.label} ${use.calls}`).join(', ')})` : ''}
        {state.model.medianSeconds != null ? `, median ${state.model.medianSeconds.toFixed(0)} s` : ''}{state.model.failed ? `, ${state.model.failed} failed` : ''}.
        {state.model.order ? ` In order: ${state.model.order}.` : ' Your Claude Code plan first, then Codex, the API last.'}</p> : null}
      <p className="pt-2 text-[11px] text-muted-text">{state.note}</p>
    </Card>
  );
}
