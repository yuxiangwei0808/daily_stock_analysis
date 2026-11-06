import { registerIndicator, registerOverlay } from 'klinecharts';
import type { KLineData, OverlayCreate, OverlayFigure, Period } from 'klinecharts';
import type { ChartBar, ChartBox, ChartFlag, ChartInterval, ChartLevel, ChartSnapshot } from '../../types/tradeDesk';

/** What the chart tab draws, built from a snapshot; KLineChart only renders these. */
export const NX_INDICATOR = 'DSA_NX';
export const PERIODS: Record<ChartInterval, Period> = {
  '1m': { type: 'minute', span: 1 },
  '5m': { type: 'minute', span: 5 },
  '15m': { type: 'minute', span: 15 },
  '1d': { type: 'day', span: 1 },
};

export interface ChartColors {
  up: string;
  down: string;
  text: string;
  muted: string;
  grid: string;
  primary: string;
  warning: string;
  purple: string;
}

interface BoxData { label: string; color: string; alive: boolean }
interface LevelData { label: string; color: string; dashed: boolean; bold?: boolean }
interface MarkData { label: string; color: string; up: boolean }
type NxRow = { a?: number | null; b?: number | null; a1?: number | null; b1?: number | null };

const ms = (iso: string) => new Date(iso).getTime();
/** ``hsl(h s l)`` with an alpha. */
export const alpha = (color: string, value: number) => color.replace(/\)$/, ` / ${value})`);
const LEVEL_KIND: Record<string, 'value' | 'prior' | 'yours'> = {
  val: 'value', vah: 'value', prior_high: 'prior', prior_low: 'prior', prior_close: 'prior', stop: 'yours', target: 'yours', alert: 'yours',
};

/** A bar's start in ms, which is where KLineChart places it. */
export function toKLine(bars: ChartBar[]): KLineData[] {
  return bars.map((bar) => ({ timestamp: ms(bar.start), open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: bar.volume }));
}

/** The NX lines by bar start, for the indicator's calc. */
export function nxByTime(snapshot: ChartSnapshot): Record<number, NxRow> {
  const out: Record<number, NxRow> = {};
  const { a = [], b = [], a1 = [], b1 = [] } = snapshot.nx || {};
  snapshot.bars.forEach((bar, i) => { out[ms(bar.start)] = { a: a[i], b: b[i], a1: a1[i], b1: b1[i] }; });
  return out;
}

const minutes = (interval: ChartInterval) => (interval === '1d' ? 0 : Number(interval.slice(0, -1)));

/** The start of the shown bar that holds a 15-minute bar ending at ``end``. */
function lastShownStart(end: string, interval: ChartInterval) {
  return ms(end) - minutes(interval) * 60000;
}

/** Prices as the page shows them: cents, or four places under a dollar. */
export const price = (value: number) => value.toFixed(Math.abs(value) < 1 ? 4 : 2);

/** The short name drawn on the chart; the side panel lists the full label. */
export function shortLabel(level: ChartLevel) {
  const week = /2 weeks ago/.test(level.label) ? ' −2w' : /last week/.test(level.label) ? ' −1w' : '';
  const names: Record<string, string> = { val: `VAL${week}`, vah: `VAH${week}`, prior_high: 'PDH', prior_low: 'PDL', prior_close: 'PDC', stop: 'Your stop', target: 'Your target', alert: 'Your alert' };
  return `${names[level.kind] || level.label} ${price(level.price)}`;
}

const NY_TIME = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
/** A bar time in New York, as the chart's axis shows it: "Nov 5, 15:00". */
export const nyTime = (iso: string) => NY_TIME.format(new Date(iso));

export function flagTitle(flag: ChartFlag) {
  return `${flag.kind === 'fade' ? 'Fade' : 'Break and retest'} ${flag.direction}`;
}

/** Overlays for a snapshot: boxes, levels, past trades, and the selected flag's entry, stop and target. */
export function overlaysFor(snapshot: ChartSnapshot, colors: ChartColors, selected?: ChartFlag | null): OverlayCreate[] {
  const out: OverlayCreate[] = [];
  const interval = snapshot.interval;
  if (interval !== '1d') {  // boxes come from 15-minute bars; a daily bar would hide them
    const lastBar = snapshot.bars.length ? ms(snapshot.bars[snapshot.bars.length - 1].start) : 0;
    snapshot.boxes.forEach((box: ChartBox) => {
      const color = box.box === 'range' ? colors.primary : colors.purple;
      const until = Math.min(lastShownStart(box.until, interval), lastBar);
      out.push({
        name: 'dsaBox', groupId: 'system', lock: true,
        points: [{ timestamp: ms(box.from), value: box.top }, { timestamp: lastShownStart(box.to, interval), value: box.bottom },
          { timestamp: Math.max(until, lastShownStart(box.to, interval)), value: box.top }],
        extendData: { label: `${box.box === 'range' ? 'Range' : 'Base'} ${price(box.bottom)}–${price(box.top)}`, color, alive: box.alive } satisfies BoxData,
      });
    });
    snapshot.flags.filter((flag) => flag.time && (flag.state === 'open' || flag.state === 'closed')).forEach((flag) => {
      const r = flag.result?.r;
      out.push({
        name: 'dsaMark', groupId: 'system', lock: true,
        points: [{ timestamp: lastShownStart(flag.time as string, interval), value: flag.price }],
        extendData: { label: `${flag.kind === 'fade' ? 'fade' : 'retest'}${r == null ? '' : ` ${r >= 0 ? '+' : '−'}${Math.abs(r).toFixed(1)}R`}`,
          color: flag.direction === 'long' ? colors.up : colors.down, up: flag.direction === 'long' } satisfies MarkData,
      });
    });
  }
  const levelColor = (level: ChartLevel) => {
    const kind = LEVEL_KIND[level.kind];
    if (level.kind === 'stop') return colors.down;
    if (level.kind === 'target') return colors.up;
    if (kind === 'yours') return colors.warning;
    return kind === 'value' ? colors.purple : colors.muted;
  };
  snapshot.levels.forEach((level) => out.push({
    name: 'dsaLevel', groupId: 'system', lock: true, points: [{ value: level.price }],
    extendData: { label: shortLabel(level), color: levelColor(level), dashed: LEVEL_KIND[level.kind] !== 'yours' } satisfies LevelData,
  }));
  if (selected) {
    const tone = selected.direction === 'long' ? colors.up : colors.down;
    const parts: Array<[string, number, string]> = [
      [`${selected.state === 'watching' ? 'Watching' : selected.state === 'armed' ? 'Armed' : 'Entry'}: ${flagTitle(selected)} ${price(selected.price)}`, selected.price, tone],
      [`Stop ${price(selected.stop)}`, selected.stop, colors.down], [`Target ${price(selected.target)}`, selected.target, colors.up]];
    parts.forEach(([label, value, color]) => out.push({
      name: 'dsaLevel', groupId: 'flag', lock: true, points: [{ value }],
      extendData: { label, color, dashed: false, bold: true } satisfies LevelData,
    }));
  }
  return out;
}

const label = (x: number, y: number, text: string, color: string, align: CanvasTextAlign = 'left', baseline: CanvasTextBaseline = 'bottom'): OverlayFigure => ({
  type: 'text', ignoreEvent: true, attrs: { x, y, text, align, baseline },
  styles: { style: 'fill', color, size: 11, backgroundColor: 'transparent', paddingLeft: 2, paddingRight: 2, paddingTop: 1, paddingBottom: 1 },
});

let registered = false;
/** The labelled box, level line and trade mark (once per page). */
export function registerChartFigures() {
  if (registered) return;
  registered = true;
  registerOverlay<BoxData>({
    name: 'dsaBox', totalStep: 4, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, coordinates, overlay }) => {
      if (coordinates.length < 3) return [];
      const half = chart.getBarSpace().halfBar;
      const [a, b, c] = coordinates;
      const { color, alive, label: text } = overlay.extendData;
      const x = a.x - half;
      const figures: OverlayFigure[] = [{
        type: 'rect', ignoreEvent: true, attrs: { x, y: a.y, width: Math.max(1, b.x + half - x), height: Math.max(1, b.y - a.y) },
        styles: { style: 'stroke_fill', color: alpha(color, 0.12), borderColor: color, borderSize: 1, borderStyle: alive ? 'solid' : 'dashed', borderDashedValue: [3, 3] },
      }, label(x, a.y - 1, text, color)];
      if (alive && c.x > b.x) {
        figures.push(...[a.y, b.y].map((y) => ({ type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: b.x + half, y }, { x: c.x + half, y }] },
          styles: { style: 'dashed', color, size: 1, dashedValue: [4, 3] } })));
      }
      return figures;
    },
  });
  registerOverlay<LevelData>({
    name: 'dsaLevel', totalStep: 2, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false,
    createPointFigures: ({ coordinates, bounding, overlay }) => {
      if (!coordinates.length) return [];
      const { y } = coordinates[0];
      const { color, dashed, bold, label: text } = overlay.extendData;
      return [{ type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: 0, y }, { x: bounding.width, y }] },
        styles: { style: dashed ? 'dashed' : 'solid', color, size: bold ? 1.5 : 1, dashedValue: [6, 4] } },
      label(bounding.width - 4, y - 1, text, color, 'right')];
    },
    createYAxisFigures: ({ coordinates, overlay }) => {
      if (!coordinates.length) return [];
      const { color } = overlay.extendData;
      const value = overlay.points[0]?.value;
      return value == null ? [] : [{ type: 'text', ignoreEvent: true, attrs: { x: 2, y: coordinates[0].y, text: price(value), align: 'left', baseline: 'middle' },
        styles: { style: 'fill', color: '#fff', size: 11, backgroundColor: color, borderRadius: 2, paddingLeft: 3, paddingRight: 3, paddingTop: 2, paddingBottom: 2 } }];
    },
  });
  registerOverlay<MarkData>({
    name: 'dsaMark', totalStep: 2, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false,
    createPointFigures: ({ coordinates, overlay }) => {
      if (!coordinates.length) return [];
      const { x, y } = coordinates[0];
      const { color, up, label: text } = overlay.extendData;
      const tip = up ? y + 3 : y - 3;
      const base = up ? tip + 8 : tip - 8;
      return [{ type: 'polygon', ignoreEvent: true, attrs: { coordinates: [{ x, y: tip }, { x: x - 5, y: base }, { x: x + 5, y: base }] }, styles: { style: 'fill', color } },
        label(x, up ? base + 1 : base - 1, text, color, 'center', up ? 'top' : 'bottom')];
    },
  });
  registerIndicator<NxRow, unknown, Record<number, NxRow>>({
    name: NX_INDICATOR, shortName: 'NX', precision: 2,
    figures: [{ key: 'a', title: 'A: ', type: 'line' }, { key: 'b', title: 'B: ', type: 'line' },
      { key: 'a1', title: 'A1: ', type: 'line' }, { key: 'b1', title: 'B1: ', type: 'line' }],
    calc: (dataList, indicator) => dataList.map((bar) => indicator.extendData?.[bar.timestamp] ?? {}),
  });
}
