import { useCallback, useEffect, useRef, useState } from 'react';
import { BellRing, Play, Square } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Badge, Button, Card } from '../common';
import type { AssistantContract, AssistantEvent, AssistantHorizon, AssistantState, AssistantTicker } from '../../types/tradeDesk';
import { nyTime, price } from './chartOverlays';
import { errorMessage } from './deskFormat';

const HORIZONS: Array<[AssistantHorizon, string]> = [['0dte', '0DTE options'], ['intraday', 'Intraday'], ['swing', 'Swing']];
const signed = (value?: number | null, digits = 2) => (value == null ? '—' : `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(digits)}%`);
const num = (value?: number | null, digits = 2) => (value == null ? '—' : value.toFixed(digits));

function canNotify() {
  return typeof window !== 'undefined' && 'Notification' in window;
}

function beep(context: AudioContext | null) {
  if (!context) return;
  try {
    const tone = context.createOscillator();
    const gain = context.createGain();
    tone.frequency.value = 880;
    gain.gain.setValueAtTime(0.15, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.25);
    tone.connect(gain).connect(context.destination);
    tone.start();
    tone.stop(context.currentTime + 0.25);
  } catch { /* sound is a courtesy */ }
}

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
          <span key={level.id} className="mr-3">{level.label} {price(level.price)}: <span className="font-mono">{num(level.now)} → {num(level.in30m)}</span></span>)}</td>
      </tr>)}</tbody>
    </table></div>
  );
}

function TickerDetail({ item }: { item: AssistantTicker }) {
  return (
    <div className="space-y-3">
      <p className="text-sm text-foreground"><span className="font-semibold">{item.ticker}</span> <span className="font-mono">{item.price == null ? '—' : price(item.price)}</span>
        <span className="text-secondary-text"> {signed(item.changePct)}{item.quoteAgeS != null ? ` · last trade ${item.quoteAgeS.toFixed(1)} s ago` : ''}</span>
        {!item.loaded ? <span className="text-muted-text"> · loading levels…</span> : null}{item.error ? <span className="text-danger"> · {item.error}</span> : null}</p>
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
              : <p className="text-xs text-muted-text">None on {item.ticker}. A fill shows here within seconds while shadow mode's feed is live.</p>}
          </div>
          {item.flags.length ? <div>
            <h4 className="pb-1 text-xs font-semibold text-foreground">Box flags</h4>
            <ul className="space-y-1 text-xs text-secondary-text">{item.flags.map((flag) => <li key={`${flag.kind}-${flag.box}-${flag.direction}-${flag.price}`}>
              <Badge variant={flag.state === 'open' ? 'warning' : 'default'}>{flag.state}</Badge> {flag.kind.replace('_', ' ')} {flag.direction} at {price(flag.price)},
              stop {price(flag.stop)}, target {price(flag.target)} <span className="text-muted-text">· {flag.grade}</span></li>)}</ul>
          </div> : null}
        </div>
      </div>
      <div>
        <h4 className="pb-1 text-xs font-semibold text-foreground">Contracts{item.contracts.expiry ? `, expiring ${item.contracts.expiry}` : ''}</h4>
        {item.contracts.rows.length ? <ContractRows rows={item.contracts.rows} />
          : <p className="text-xs text-muted-text">{item.contracts.error ? `No contracts: ${item.contracts.error}` : 'Loading the chain…'}</p>}
      </div>
    </div>
  );
}

/** The trading assistant: start it on one to three stocks; levels, contracts, your positions and alerts, live. */
export function AssistantPanel({ current, onShow }: { current: string; onShow: (ticker: string) => void }) {
  const [state, setState] = useState<AssistantState | null>(null);
  const [names, setNames] = useState(current);
  const [horizon, setHorizon] = useState<AssistantHorizon>('0dte');
  const [focus, setFocus] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [permission, setPermission] = useState(canNotify() ? Notification.permission : 'unsupported');
  const audio = useRef<AudioContext | null>(null);
  const seen = useRef(0);

  useEffect(() => {
    const load = async () => {
      try { setState(await tradeDeskApi.getAssistant()); } catch { setState(null); }  // the desk is optional
    };
    void load();
  }, []);
  useEffect(() => { if (current) setNames((value) => value || current); }, [current]);

  const announce = useCallback((event: AssistantEvent) => {
    if (event.seq <= seen.current) return;
    seen.current = event.seq;
    if (event.kind === 'session') return;
    beep(audio.current);
    if (canNotify() && Notification.permission === 'granted') {
      try { new Notification(event.title, { body: event.detail, tag: `dsa-assistant-${event.seq}` }); } catch { /* the page's feed still shows it */ }
    }
  }, []);

  const available = state !== null;
  useEffect(() => {
    if (!available || typeof EventSource === 'undefined') return undefined;  // only once the desk answered
    const source = new EventSource(tradeDeskApi.getAssistantStreamUrl(), { withCredentials: true });
    const read = <T,>(event: Event): T | null => {
      try { return tradeDeskApi.readAssistant<T>(JSON.parse((event as MessageEvent<string>).data)); } catch { return null; }
    };
    source.addEventListener('state', (event) => { const next = read<AssistantState>(event); if (next) setState(next); });
    source.addEventListener('alert', (event) => { const next = read<AssistantEvent>(event); if (next) announce(next); });
    return () => source.close();
  }, [announce, available]);

  const start = async () => {
    const tickers = names.split(/[\s,]+/).map((name) => name.trim().toUpperCase()).filter(Boolean);
    if (!tickers.length) { setError('Enter one to three stocks'); return; }
    if (!audio.current && typeof window !== 'undefined' && 'AudioContext' in window) {
      try { audio.current = new AudioContext(); } catch { audio.current = null; }  // created on the click, so it may play
    }
    setBusy(true); setError('');
    try {
      const next = await tradeDeskApi.startAssistant(tickers, horizon);
      seen.current = next.seq;
      setState(next); setFocus(next.tickers[0]?.ticker || '');
      if (next.tickers[0]) onShow(next.tickers[0].ticker);
    } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(false); }
  };
  const stop = async () => {
    setBusy(true); setError('');
    try { setState(await tradeDeskApi.stopAssistant()); } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(false); }
  };
  const allow = async () => {
    if (!canNotify()) return;
    try { setPermission(await Notification.requestPermission()); } catch { setPermission(Notification.permission); }
  };

  if (!state) return null;
  const running = state.running;
  const shown = state.tickers.find((item) => item.ticker === focus) || state.tickers[0];
  return (
    <Card variant="bordered" padding="md" data-testid="assistant-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-foreground">Trading assistant{running ? `: ${state.tickers.map((item) => item.ticker).join(', ')} · ${state.horizons[state.horizon || '0dte']}${state.startedAt ? ` · since ${nyTime(state.startedAt)} ET` : ''}` : ''}</h3>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {permission === 'default' ? <Button size="xsm" variant="ghost" onClick={() => void allow()}><BellRing className="h-3.5 w-3.5" />Allow notifications</Button> : null}
          {permission === 'denied' ? <span className="text-muted-text">Notifications are blocked in this browser; alerts still sound here.</span> : null}
          {running ? <Button size="xsm" variant="outline" isLoading={busy} onClick={() => void stop()}><Square className="h-3.5 w-3.5" />Stop</Button> : null}
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
        <Button type="submit" size="xsm" variant="outline" isLoading={busy}><Play className="h-3.5 w-3.5" />Start</Button>
      </form> : null}
      {error ? <p role="alert" className="pt-1 text-xs text-danger">{error}</p> : null}
      {!running ? <p className="pt-2 text-[11px] text-muted-text">Nothing runs until you press Start. It watches the stocks until you stop it or 15 minutes after the close: the day's levels, the option contracts around the price, your positions on them, and an alert with a sound on this page when something changes.{state.stopped ? ` Last session: ${state.stopped.reason} at ${nyTime(state.stopped.at)} ET.` : ''}</p> : null}
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
      <p className="pt-2 text-[11px] text-muted-text">{state.note}</p>
    </Card>
  );
}
