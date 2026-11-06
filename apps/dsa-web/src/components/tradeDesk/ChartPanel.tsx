import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { dispose, init } from 'klinecharts';
import type { Chart, DeepPartial, KLineData, Styles } from 'klinecharts';
import { CandlestickChart, RefreshCw } from 'lucide-react';
import { systemConfigApi } from '../../api/systemConfig';
import { tradeDeskApi, tradeDeskTicker } from '../../api/tradeDesk';
import { Badge, Button, Card, EmptyState, InlineAlert } from '../common';
import type { ChartFlag, ChartInterval, ChartSnapshot } from '../../types/tradeDesk';
import { NX_INDICATOR, PERIODS, alpha, flagTitle, nxByTime, nyTime, overlaysFor, price, registerChartFigures, toKLine } from './chartOverlays';
import type { ChartColors } from './chartOverlays';
import { errorMessage, formatDate } from './deskFormat';
import { GradesCard, YoursCard } from './ChartYours';
import { AssistantPanel } from './AssistantPanel';
import { StopRiskCard } from './StopRiskCard';

const INTERVALS: ChartInterval[] = ['1m', '5m', '15m', '1d'];
const VARIANTS = [
  { key: '1-normal', label: 'Impulse 1 T', impulse: 1.0, tidy: false },
  { key: '0.6-normal', label: 'Impulse 0.6 T', impulse: 0.6, tidy: false },
  { key: '1-tidy', label: 'Impulse 1 T, tidy boxes', impulse: 1.0, tidy: true },
  { key: '0.6-tidy', label: 'Impulse 0.6 T, tidy boxes', impulse: 0.6, tidy: true },
];
const REFRESH_MS = 60000;  // without the live stream (it failed, or more than five charts are open)
const LIVE_QUIET_MS = 60000;
type LiveState = 'off' | 'connecting' | 'connected' | 'evicted';
const STATE_LABEL: Record<ChartFlag['state'], string> = { armed: 'Armed', watching: 'Watching', open: 'Open', closed: 'Closed' };
const STATE_HELP: Record<ChartFlag['state'], string> = {
  armed: 'a limit the next 15-minute bar fills at this price',
  watching: 'waits for a 15-minute close past this price, then a retest',
  open: 'taken; R is at the last close',
  closed: 'finished by its stop, target or time limit',
};
const WARNINGS: Record<string, string> = {
  nx_needs_250_bars: 'NX needs 250 bars; fewer are loaded at this interval.',
  boxes_need_11_sessions: 'Boxes need 11 sessions of 15-minute bars; this stock has fewer.',
  no_completed_15m_bars: 'No completed 15-minute bars yet.',
};
const BOX_HIDDEN = 'Boxes come from 15-minute bars: switch to 15m, 5m or 1m to see them. Lines are drawn at every interval.';

const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function readColors(): ChartColors {
  const hsl = (name: string) => `hsl(${css(name)})`;
  return { up: hsl('--color-success'), down: hsl('--color-danger'), text: hsl('--secondary-text'), muted: hsl('--muted-text'),
    grid: `hsl(${css('--border')} / 0.45)`, primary: hsl('--primary'), warning: hsl('--color-warning'), purple: hsl('--color-purple') };
}

/** The theme's colors for the canvas, re-read when light and dark mode switch. */
function useChartColors(): ChartColors {
  const [colors, setColors] = useState(readColors);
  useEffect(() => {
    const observer = new MutationObserver(() => setColors(readColors()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] });
    return () => observer.disconnect();
  }, []);
  return colors;
}

function chartStyles(colors: ChartColors, compact: boolean): DeepPartial<Styles> {
  const axis = { axisLine: { color: colors.grid }, tickLine: { color: colors.grid }, tickText: { color: colors.text } };
  return {
    grid: { horizontal: { color: colors.grid }, vertical: { color: colors.grid } },
    candle: {
      bar: { upColor: colors.up, downColor: colors.down, noChangeColor: colors.muted, upBorderColor: colors.up, downBorderColor: colors.down,
        noChangeBorderColor: colors.muted, upWickColor: colors.up, downWickColor: colors.down, noChangeWickColor: colors.muted },
      priceMark: { high: { color: colors.text }, low: { color: colors.text }, last: { upColor: colors.up, downColor: colors.down, noChangeColor: colors.muted } },
      tooltip: { showRule: compact ? 'follow_cross' : 'always', title: { color: colors.text }, legend: { color: colors.text } },
    },
    indicator: {
      bars: [{ upColor: alpha(colors.up, 0.55), downColor: alpha(colors.down, 0.55), noChangeColor: colors.muted }],
      lines: [{ color: colors.warning }, { color: alpha(colors.warning, 0.6) }, { color: colors.purple }, { color: alpha(colors.purple, 0.6) }],
      tooltip: { showRule: compact ? 'follow_cross' : 'always', title: { color: colors.text }, legend: { color: colors.text } },
    },
    xAxis: axis, yAxis: axis, separator: { color: colors.grid },
  };
}

const pct = (value: number) => `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(2)}%`;
const r = (value: number) => `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(2)}R`;
const flagKey = (flag: ChartFlag) => `${flag.setup}|${flag.kind}|${flag.box}|${flag.state}|${flag.time ?? ''}`;

/** The canvas: candles, volume and NX, with the snapshot's overlays. */
function PriceChart({ snapshot, selected, colors, live, handleRef }: { snapshot: ChartSnapshot; selected: ChartFlag | null; colors: ChartColors;
  live?: KLineData | null; handleRef?: React.MutableRefObject<Chart | null> }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const barsRef = useRef<KLineData[]>([]);
  const pushRef = useRef<((bar: KLineData) => void) | null>(null);
  const shownRef = useRef('');

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;
    registerChartFigures();
    const chart = init(container, { timezone: 'America/New_York' });
    if (!chart) return undefined;
    chartRef.current = chart;
    if (handleRef) handleRef.current = chart;
    chart.setDataLoader({
      getBars: ({ type, callback }) => callback(type === 'init' ? barsRef.current : [], false),
      subscribeBar: ({ callback }) => { pushRef.current = callback; },
      unsubscribeBar: () => { pushRef.current = null; },
    });
    chart.createIndicator('VOL');
    chart.createIndicator({ name: NX_INDICATOR, paneId: 'candle_pane' }, true);
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => chart.resize()) : null;
    observer?.observe(container);
    return () => { observer?.disconnect(); dispose(container); chartRef.current = null; shownRef.current = ''; if (handleRef) handleRef.current = null; };
  }, [handleRef]);

  useEffect(() => {
    const chart = chartRef.current;
    // On a phone the always-on legend would cover the candles: it shows while the crosshair is used.
    if (chart) chart.setStyles(chartStyles(colors, (containerRef.current?.clientWidth ?? 800) < 640));
  }, [colors]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    barsRef.current = toKLine(snapshot.bars);
    chart.overrideIndicator({ name: NX_INDICATOR, extendData: nxByTime(snapshot) });
    const precision = snapshot.bars.some((bar) => bar.close < 1) ? 4 : 2;
    const shown = `${snapshot.ticker}|${snapshot.interval}`;
    if (shownRef.current !== shown) {
      shownRef.current = shown;
      chart.setSymbol({ ticker: snapshot.ticker, pricePrecision: precision, volumePrecision: 0 });
      chart.setPeriod(PERIODS[snapshot.interval]);
    } else {
      chart.resetData();
    }
  }, [snapshot]);

  // A pushed bar updates the forming candle, or starts the next one.
  useEffect(() => { if (live) pushRef.current?.(live); }, [live]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.removeOverlay({ groupId: 'system' });
    chart.removeOverlay({ groupId: 'flag' });
    chart.createOverlay(overlaysFor(snapshot, colors, selected));
  }, [snapshot, colors, selected]);

  return <div ref={containerRef} data-testid="price-chart" className="h-[360px] w-full sm:h-[460px]" />;
}

function FlagRow({ flag, last, selected, onSelect }: { flag: ChartFlag; last: number | null; selected: boolean; onSelect: () => void }) {
  const distance = last ? (flag.price / last - 1) * 100 : null;
  return (
    <button type="button" onClick={onSelect} aria-pressed={selected}
      className={`w-full rounded-xl border px-3 py-2 text-left text-xs transition ${selected ? 'border-cyan/50 bg-cyan/5' : 'border-border/40 hover:border-border'}`}>
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant={flag.state === 'armed' ? 'info' : flag.state === 'open' ? 'warning' : 'default'}>{STATE_LABEL[flag.state]}</Badge>
        <span className="font-medium text-foreground">{flagTitle(flag)}</span>
        <span className="text-muted-text">({flag.box} box)</span>
      </div>
      <p className="mt-1 font-mono text-secondary-text">
        {price(flag.price)} · stop {price(flag.stop)} · target {price(flag.target)}{flag.rewardRisk != null ? ` · ${flag.rewardRisk}:1` : ''}
        {distance != null && (flag.state === 'armed' || flag.state === 'watching') ? ` · ${pct(distance)} from last` : ''}
      </p>
      {flag.result ? <p className="mt-0.5 text-secondary-text">{flag.state === 'closed' ? `${flag.result.how} at ${flag.result.exit == null ? '—' : price(flag.result.exit)}: ` : 'Now '}
        <strong style={{ color: flag.result.r >= 0 ? 'hsl(var(--color-success))' : 'hsl(var(--color-danger))' }}>{r(flag.result.r)}</strong>
        {flag.time ? <span className="text-muted-text"> · entered in the 15-minute bar from {nyTime(new Date(Date.parse(flag.time) - 900000).toISOString())} ET</span> : null}</p> : null}
      <p className="mt-0.5 text-muted-text">{STATE_HELP[flag.state]}. {flag.grade.label}.</p>
    </button>
  );
}

/** Pick a stock and see what the system sees: boxes, lines, NX and its graded flags. Read only. */
export function ChartPanel({ initialTicker = '' }: { initialTicker?: string }) {
  const colors = useChartColors();
  const [input, setInput] = useState(initialTicker);
  const [ticker, setTicker] = useState(initialTicker);
  const [barInterval, setBarInterval] = useState<ChartInterval>('15m');
  const [variant, setVariant] = useState(VARIANTS[0].key);
  const [snapshot, setSnapshot] = useState<ChartSnapshot | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [showClosed, setShowClosed] = useState(false);
  const [picks, setPicks] = useState<{ held: string[]; watchlist: string[]; ideas: string[] }>({ held: [], watchlist: [], ideas: [] });
  const sequence = useRef(0);
  const chartHandle = useRef<Chart | null>(null);
  const [gradesVersion, setGradesVersion] = useState(0);

  useEffect(() => {
    void Promise.allSettled([tradeDeskApi.getHoldings(), systemConfigApi.getWatchlist(), tradeDeskApi.getTrackRecord()]).then(([held, watch, record]) => {
      const unique = (values: Array<string | null | undefined>) => Array.from(new Set(values.filter((value): value is string => Boolean(value))));
      setPicks({
        held: held.status === 'fulfilled' && held.value.view
          ? unique([...(held.value.view.stocks || []).map((row) => row.ticker), ...(held.value.view.options || []).filter((row) => !row.expired).map((row) => row.underlying)]) : [],
        watchlist: watch.status === 'fulfilled' ? unique(watch.value.map(tradeDeskTicker)).slice(0, 20) : [],
        ideas: record.status === 'fulfilled' ? unique((record.value.recent || []).filter((item) => item.kind === 'idea').map((item) => tradeDeskTicker(item.ticker))).slice(0, 10) : [],
      });
    });
  }, []);

  const params = VARIANTS.find((item) => item.key === variant) || VARIANTS[0];
  const load = useCallback(async (quiet = false) => {
    if (!ticker) return;
    const mine = ++sequence.current;
    if (!quiet) setBusy(true);
    try {
      const data = await tradeDeskApi.getChart(ticker, barInterval, params.impulse, params.tidy);
      if (mine !== sequence.current) return;
      setSnapshot(data);
      setError('');
    } catch (loadError) {
      if (mine === sequence.current) setError(errorMessage(loadError));
    } finally {
      if (mine === sequence.current) setBusy(false);
    }
  }, [ticker, barInterval, params.impulse, params.tidy]);

  useEffect(() => { void load(); }, [load]);
  const loadRef = useRef(load);
  useEffect(() => { loadRef.current = load; }, [load]);

  // Live: moomoo's pushes for the forming bar and the last trade; at each 15-minute close the snapshot is reloaded.
  const [liveState, setLiveState] = useState<LiveState>('off');
  const [liveBar, setLiveBar] = useState<KLineData | null>(null);
  const [quote, setQuote] = useState<{ price: number; at: string | null; received: number } | null>(null);
  const shown = snapshot && snapshot.ticker === ticker && snapshot.interval === barInterval;
  useEffect(() => {
    setLiveBar(null); setQuote(null);
    if (!shown || typeof EventSource === 'undefined') { setLiveState('off'); return undefined; }
    const source = new EventSource(tradeDeskApi.getChartStreamUrl(ticker, barInterval), { withCredentials: true });
    setLiveState('connecting');
    const read = (event: Event) => { try { return JSON.parse((event as MessageEvent<string>).data); } catch { return null; } };
    source.addEventListener('ready', () => setLiveState('connected'));
    source.addEventListener('bar', (event) => {
      const data = read(event);
      if (data?.interval === barInterval && data.bar) setLiveBar(toKLine([data.bar])[0]);
    });
    source.addEventListener('quote', (event) => {
      const data = read(event);
      if (data && typeof data.price === 'number') setQuote({ price: data.price, at: data.at ?? null, received: Date.now() });
    });
    source.addEventListener('closed', () => { void loadRef.current(true); });
    source.addEventListener('evicted', () => { setLiveState('evicted'); source.close(); });
    source.onerror = () => setLiveState((state) => (state === 'evicted' ? state : 'connecting'));  // EventSource retries
    return () => source.close();
  }, [shown, ticker, barInterval]);

  useEffect(() => {
    if (!ticker || liveState === 'connected') return undefined;
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load(true); }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [ticker, load, liveState]);
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    if (liveState !== 'connected') return undefined;
    const timer = window.setInterval(() => setClock(Date.now()), 5000);
    return () => window.clearInterval(timer);
  }, [liveState]);

  const show = (value: string) => {
    const next = tradeDeskTicker(value);
    if (!next) { setError('Enter a US ticker, for example SPY'); return; }
    setInput(next); setError('');
    if (next !== ticker) { setSnapshot(null); setSelectedKey(null); }
    setTicker(next);
  };

  const flags = snapshot?.flags || [];
  const current = flags.filter((flag) => flag.state !== 'closed');
  const closed = flags.filter((flag) => flag.state === 'closed');
  const selected = flags.find((flag) => flagKey(flag) === selectedKey) || current[0] || null;
  const lastBar = snapshot?.bars.length ? snapshot.bars[snapshot.bars.length - 1] : null;
  const lastPrice = quote?.price ?? liveBar?.close ?? lastBar?.close ?? null;
  const last = lastPrice == null ? null : { close: lastPrice };
  const pushing = liveState === 'connected' && quote != null && clock - quote.received < LIVE_QUIET_MS;
  const liveLine = liveState === 'evicted' ? 'Live updates paused: more than five charts are open. Refreshing every minute.'
    : liveState === 'connecting' ? 'Connecting to live updates…'
      : liveState === 'connected' ? (pushing ? `Live · last trade ${quote?.at ? nyTime(quote.at) : ''} ET` : 'Live: waiting for trades (outside the regular session there are none)')
        : '';
  const closedR = useMemo(() => closed.reduce((sum, flag) => sum + (flag.result?.r || 0), 0), [closed]);
  const pickGroups: Array<[string, string[]]> = [['Holdings', picks.held], ['Watchlist', picks.watchlist], ['Trade ideas', picks.ideas]];

  return (
    <div className="space-y-4" data-testid="chart-panel">
      <Card variant="bordered" padding="md">
        <form className="flex flex-wrap items-end gap-3" onSubmit={(event) => { event.preventDefault(); show(input); }}>
          <label className="flex flex-col gap-1 text-xs text-secondary-text">Ticker
            <input aria-label="Chart ticker" value={input} onChange={(event) => setInput(event.target.value.toUpperCase())} placeholder="SPY"
              className="input-surface h-9 w-28 rounded-lg border px-3 font-mono text-sm text-foreground" />
          </label>
          <Button type="submit" size="sm" variant="outline"><CandlestickChart className="h-4 w-4" />Show</Button>
          <div className="flex gap-1 rounded-lg border border-border/50 p-1 text-xs" role="group" aria-label="Interval">
            {INTERVALS.map((key) => <button key={key} type="button" aria-pressed={barInterval === key} onClick={() => setBarInterval(key)}
              className={`rounded-md px-2.5 py-1 text-xs ${barInterval === key ? 'bg-cyan/10 text-cyan' : 'text-secondary-text hover:text-foreground'}`}>{key}</button>)}
          </div>
          <label className="flex flex-col gap-1 text-xs text-secondary-text">Boxes
            <select aria-label="Box rules" value={variant} onChange={(event) => setVariant(event.target.value)}
              className="input-surface h-9 rounded-lg border px-2 text-sm text-foreground">
              {VARIANTS.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
            </select>
          </label>
          {ticker ? <Button type="button" size="sm" variant="ghost" isLoading={busy} onClick={() => void load()}><RefreshCw className="h-4 w-4" />Refresh</Button> : null}
        </form>
        <div className="mt-3 space-y-1.5">
          {pickGroups.filter(([, list]) => list.length).map(([name, list]) => (
            <div key={name} className="flex flex-wrap items-center gap-1.5 text-xs">
              <span className="w-24 shrink-0 text-muted-text">{name}</span>
              {list.map((item) => <button key={item} type="button" onClick={() => show(item)}
                className={`rounded-md border px-2 py-0.5 font-mono ${item === ticker ? 'border-cyan/50 text-cyan' : 'border-border/50 text-secondary-text hover:text-foreground'}`}>{item}</button>)}
            </div>
          ))}
        </div>
      </Card>
      <AssistantPanel current={ticker} onShow={show} />
      {error ? <InlineAlert variant="danger" message={error} /> : null}
      {!ticker ? <Card variant="bordered" padding="md"><EmptyState icon={<CandlestickChart className="h-8 w-8" />} title="Pick a stock"
        description="Type a ticker or pick one from your holdings, watchlist or trade ideas. The chart shows the system's boxes, value areas, prior-day levels, your stops and alerts, and NX." /></Card> : null}
      {snapshot ? (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px] lg:items-start">
          <Card variant="bordered" padding="sm" className="min-w-0">
            <div className="flex flex-wrap items-baseline justify-between gap-2 px-1 pb-2">
              <div className="flex flex-wrap items-baseline gap-2">
                <h2 className="font-mono text-lg font-semibold text-foreground">{snapshot.ticker}</h2>
                {last ? <span className="font-mono text-sm text-foreground">{price(last.close)}</span> : null}
                <span className="text-xs text-muted-text">{snapshot.interval} bars · boxes as of {formatDate(snapshot.asOf)}</span>
                {liveLine ? <span data-testid="live-state" className={`flex items-center gap-1 text-xs ${pushing ? 'text-success' : 'text-muted-text'}`}>
                  {pushing ? <span className="h-1.5 w-1.5 rounded-full bg-success" /> : null}{liveLine}</span> : null}
              </div>
              {snapshot.typicalRange ? <span className="text-xs text-muted-text">Typical session range T = {price(snapshot.typicalRange.t)}; an impulse is a 15-minute move of {price(snapshot.typicalRange.impulse)} or more</span> : null}
            </div>
            <PriceChart snapshot={snapshot} selected={selected} colors={colors} live={liveBar} handleRef={chartHandle} />
            <p className="px-1 pt-2 text-[11px] text-muted-text">{snapshot.note}</p>
            {snapshot.interval === '1d' ? <p className="px-1 pt-1 text-[11px] text-muted-text">{BOX_HIDDEN}</p> : null}
            {snapshot.warnings.map((code) => <p key={code} className="px-1 pt-1 text-[11px] text-warning">{WARNINGS[code] || code}</p>)}
          </Card>
          <div className="space-y-4">
            <Card variant="bordered" padding="sm">
              <h3 className="px-1 pb-2 text-sm font-semibold text-foreground">Flags</h3>
              {current.length ? <div className="space-y-2 text-xs">{current.map((flag) => <FlagRow key={flagKey(flag)} flag={flag} last={last?.close ?? null}
                selected={selected === flag} onSelect={() => setSelectedKey(flagKey(flag))} />)}</div>
                : <p className="px-1 text-xs text-secondary-text">No live setup: no box is armed, waiting for a break, or in a trade.</p>}
              {closed.length ? <div className="mt-3 border-t border-border/40 pt-2 text-xs">
                <button type="button" className="px-1 text-xs text-secondary-text hover:text-foreground" aria-expanded={showClosed} onClick={() => setShowClosed(!showClosed)}>
                  {showClosed ? 'Hide' : 'Show'} {closed.length} finished trade{closed.length === 1 ? '' : 's'} in this window ({r(closedR)} in total)</button>
                {showClosed ? <div className="mt-2 max-h-[560px] space-y-2 overflow-y-auto">{closed.map((flag) => <FlagRow key={flagKey(flag)} flag={flag} last={last?.close ?? null}
                  selected={selected === flag} onSelect={() => setSelectedKey(flagKey(flag))} />)}</div> : null}
              </div> : null}
            </Card>
            <StopRiskCard ticker={snapshot.ticker} />
            <YoursCard ticker={snapshot.ticker} lastPrice={lastPrice} yours={snapshot.yours || []} chart={chartHandle}
              onChanged={() => { void load(true); setGradesVersion((value) => value + 1); }} />
            <Card variant="bordered" padding="sm">
              <h3 className="px-1 pb-2 text-sm font-semibold text-foreground">Lines</h3>
              {snapshot.levels.length ? <table className="w-full text-xs"><tbody>
                {[...snapshot.levels].sort((a, b) => b.price - a.price).map((level) => <tr key={`${level.kind}-${level.label}-${level.price}`} className="border-t border-border/30">
                  <td className="py-1 pr-2 text-secondary-text">{level.label}</td>
                  <td className="py-1 text-right font-mono text-foreground">{price(level.price)}</td>
                  <td className="py-1 pl-2 text-right font-mono text-muted-text">{last ? pct((level.price / last.close - 1) * 100) : ''}</td>
                </tr>)}
              </tbody></table> : <p className="px-1 text-xs text-secondary-text">No lines yet.</p>}
            </Card>
          </div>
        </div>
      ) : ticker && busy ? <p className="flex items-center gap-2 text-sm text-secondary-text"><RefreshCw className="h-4 w-4 animate-spin text-cyan" />Loading {ticker} from moomoo…</p> : null}
      <GradesCard version={gradesVersion} />
    </div>
  );
}
