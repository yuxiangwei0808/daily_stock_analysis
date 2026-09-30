import { Area, CartesianGrid, ComposedChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { PayoffPoint, StrategyCandidate } from '../../types/tradeDesk';
import { extremeRange, focusPayoffPoints, niceTicks, payoffTable, pnlAt, samplePayoff } from '../../utils/payoff';

const GAIN = 'hsl(var(--color-success))';
const LOSS = 'hsl(var(--color-danger))';
const LEVEL = 'hsl(var(--color-warning))';
const NOW = 'hsl(var(--primary))';
const MUTED = 'hsl(var(--muted-text))';

const price = (value: number) => value.toFixed(2);
const signedPct = (value: number) => (Math.abs(value) < 0.05 ? '0.0%' : `${value > 0 ? '+' : '−'}${Math.abs(value).toFixed(1)}%`);
const pnlText = (value: number) => {
  const size = Math.abs(value);
  const digits = size >= 100 || Number.isInteger(size) ? 0 : 2;
  return `${value > 0.004 ? '+' : value < -0.004 ? '−' : ''}$${size.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
};
const axisMoney = (value: number) => {
  const size = Math.abs(value);
  const text = size >= 1000 ? `${(size / 1000).toFixed(size >= 10000 ? 0 : 1)}k` : size.toFixed(0);
  return `${value < 0 ? '−' : ''}$${text}`;
};
const tone = (value: number) => (value > 0.004 ? GAIN : value < -0.004 ? LOSS : MUTED);

interface Basis { amount: number; label: string }

/** What a P/L percentage is measured against: the debit paid, or the most a credit trade can lose. */
function basisOf(candidate: StrategyCandidate): Basis | null {
  const { entryDebit, fees, lossBound, maxLoss } = candidate.payoff;
  if (entryDebit > 0) return { amount: entryDebit + (fees || 0), label: 'of cost' };
  if (lossBound === 'bounded' && maxLoss && maxLoss > 0) return { amount: maxLoss, label: 'of max loss' };
  return null;
}

function PayoffTooltip({ active, payload, now, basis }: {
  active?: boolean; payload?: Array<{ payload: PayoffPoint }>; now: number | null; basis: Basis | null;
}) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  return (
    <div className="max-w-[240px] rounded-lg border border-border/60 bg-card px-3 py-2 text-xs shadow-lg">
      <div className="text-secondary-text">At expiry, price {price(point.price)}{now ? ` (${signedPct((point.price / now - 1) * 100)} from now)` : ''}</div>
      <div className="mt-0.5 font-semibold" style={{ color: tone(point.pnl) }}>
        {pnlText(point.pnl)}{basis ? ` · ${signedPct((point.pnl / basis.amount) * 100)} ${basis.label}` : ''}
      </div>
    </div>
  );
}

/** The expiry payoff: a shaded profit/loss curve with the current price and breakevens marked, and a P/L table. */
export function PayoffPanel({ candidate, spot }: { candidate: StrategyCandidate; spot?: number | null }) {
  const { t } = useUiLanguage();
  const payoff = candidate.payoff;
  const points = payoff.points || [];
  const strikes = candidate.legs.filter((leg) => leg.right !== 'stock').map((leg) => Number(leg.strike)).filter((x) => Number.isFinite(x) && x > 0);
  const breakevens = (payoff.breakevens || []).map(Number).filter((x) => Number.isFinite(x) && x > 0);
  const now = spot != null && Number.isFinite(spot) && spot > 0 ? spot : null;
  // Keep the current price and a ±10% move in view along with the strikes and breakevens.
  const window = focusPayoffPoints(points, [...strikes, ...breakevens, ...(now ? [now * 0.9, now, now * 1.1] : [])]);
  if (window.length < 2) return <p className="text-sm text-secondary-text">No payoff curve is available.</p>;
  const from = window[0].price;
  const to = window[window.length - 1].price;
  const data = samplePayoff(points, from, to, [...strikes, ...breakevens, ...(now ? [now] : [])]);
  const values = data.map((point) => point.pnl);
  const high = Math.max(...values, 0);
  const low = Math.min(...values, 0);
  const zero = high - low > 0 ? high / (high - low) : 0.5;  // where 0 sits in the shaded area, top to bottom
  const yTicks = niceTicks(low, high, 4, true);
  const id = candidate.id.replace(/[^a-zA-Z0-9_-]/g, '');
  const basis = basisOf(candidate);
  const rows = payoffTable(points, now, breakevens, strikes);
  const unboundedAbove = payoff.gainBound === 'unbounded' || payoff.lossBound === 'unbounded';
  const gainWhere = payoff.gainBound === 'bounded' && payoff.maxGain != null ? extremeRange(points, payoff.maxGain, unboundedAbove) : '';
  const lossWhere = payoff.lossBound === 'bounded' && payoff.maxLoss != null ? extremeRange(points, -payoff.maxLoss, unboundedAbove) : '';
  const atNow = now ? pnlAt(points, now) : null;
  const bound = (kind: string, value: number | null | undefined, sign: 1 | -1) => (
    kind === 'unbounded' ? t('tradeDesk.unlimited') : kind === 'unknown' || value == null ? t('tradeDesk.unknown') : pnlText(sign * value)
  );

  return (
    <div data-testid="payoff-panel">
      <div className="flex flex-wrap gap-2 text-xs">
        {breakevens.length ? breakevens.map((level) => (
          <span key={`be-${level}`} className="max-w-full rounded-lg border px-2.5 py-1.5 text-foreground" style={{ borderColor: LEVEL }}>
            <span className="text-secondary-text">Breakeven </span><strong>{price(level)}</strong>
            {now ? <span className="text-secondary-text"> · {signedPct((level / now - 1) * 100)} from now</span> : null}
          </span>
        )) : <span className="rounded-lg border border-border/60 px-2.5 py-1.5 text-secondary-text">No breakeven</span>}
        <span className="max-w-full rounded-lg bg-elevated/60 px-2.5 py-1.5">
          <span className="text-secondary-text">Max gain </span><strong style={{ color: GAIN }}>{bound(payoff.gainBound, payoff.maxGain, 1)}</strong>
          {gainWhere ? <span className="text-secondary-text"> {gainWhere}</span> : null}
        </span>
        <span className="max-w-full rounded-lg bg-elevated/60 px-2.5 py-1.5">
          <span className="text-secondary-text">Max loss </span><strong style={{ color: LOSS }}>{bound(payoff.lossBound, payoff.maxLoss, -1)}</strong>
          {lossWhere ? <span className="text-secondary-text"> {lossWhere}</span> : null}
        </span>
        {atNow != null && now ? (
          <span className="max-w-full rounded-lg bg-elevated/60 px-2.5 py-1.5">
            <span className="text-secondary-text">If it expired at today's {price(now)} </span><strong style={{ color: tone(atNow) }}>{pnlText(atNow)}</strong>
          </span>
        ) : null}
      </div>

      <div className="mt-3 space-y-3">
        <div className="h-72 rounded-xl border border-border/50 bg-card/30 p-2" data-testid="payoff-chart">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data} margin={{ top: 18, right: 12, left: 4, bottom: 4 }}>
              <defs>
                <linearGradient id={`payoff-fill-${id}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset={0} stopColor={GAIN} stopOpacity={0.35} />
                  <stop offset={zero} stopColor={GAIN} stopOpacity={0.08} />
                  <stop offset={zero} stopColor={LOSS} stopOpacity={0.08} />
                  <stop offset={1} stopColor={LOSS} stopOpacity={0.35} />
                </linearGradient>
                <linearGradient id={`payoff-line-${id}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset={0} stopColor={GAIN} />
                  <stop offset={zero} stopColor={GAIN} />
                  <stop offset={zero} stopColor={LOSS} />
                  <stop offset={1} stopColor={LOSS} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.5} />
              <XAxis type="number" dataKey="price" domain={[from, to]} ticks={niceTicks(from, to)} allowDataOverflow
                tickFormatter={(value: number) => (Number.isInteger(value) ? String(value) : value.toFixed(value % 0.5 === 0 ? 1 : 2))}
                stroke={MUTED} tick={{ fill: MUTED }} fontSize={10} />
              <YAxis domain={[yTicks[0] ?? low, yTicks[yTicks.length - 1] ?? high]}
                ticks={yTicks} tickFormatter={axisMoney} stroke={MUTED} tick={{ fill: MUTED }} fontSize={10} width={52} />
              <Tooltip content={<PayoffTooltip now={now} basis={basis} />} cursor={{ stroke: MUTED, strokeDasharray: '3 3' }} />
              {strikes.map((strike) => <ReferenceLine key={`k-${strike}`} x={strike} stroke={MUTED} strokeDasharray="2 4" opacity={0.6} />)}
              <ReferenceLine y={0} stroke={MUTED} strokeWidth={1.5} />
              {breakevens.filter((level) => level >= from && level <= to).map((level) => (
                <ReferenceLine key={`be-${level}`} x={level} stroke={LEVEL} strokeDasharray="4 3"
                  label={{ value: `BE ${price(level)}`, position: 'insideBottomRight', fill: LEVEL, fontSize: 10 }} />
              ))}
              {now ? <ReferenceLine x={now} stroke={NOW} strokeWidth={1.5} label={{ value: `Now ${price(now)}`, position: 'top', fill: NOW, fontSize: 10 }} /> : null}
              <Area type="linear" dataKey="pnl" baseValue={0} stroke={`url(#payoff-line-${id})`} strokeWidth={2}
                fill={`url(#payoff-fill-${id})`} isAnimationActive={false} activeDot={{ r: 3 }} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
        <div className="overflow-x-auto rounded-xl border border-border/50">
          <table className="w-full whitespace-nowrap text-left text-xs" data-testid="payoff-table">
            <thead className="bg-elevated/50 text-secondary-text">
              <tr>
                <th className="px-2 py-2 sm:px-3">Price<span className="hidden sm:inline"> at expiry</span></th>
                <th className="px-2 py-2 text-right sm:px-3">{now ? <>Move<span className="hidden sm:inline"> from now</span></> : ''}</th>
                <th className="px-2 py-2 text-right sm:px-3">P/L</th>
                {basis ? <th className="hidden px-2 py-2 text-right sm:table-cell sm:px-3">%<span className="hidden sm:inline"> {basis.label}</span></th> : null}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.kind}-${row.price}`} className={`border-t border-border/40 ${row.kind === 'now' ? 'bg-elevated/60' : ''}`}>
                  <td className="px-2 py-1.5 font-mono text-foreground sm:px-3">
                    {price(row.price)}
                    {row.label ? <span className="block font-sans text-[10px] uppercase tracking-wide sm:ml-2 sm:inline" style={{ color: row.kind === 'now' ? NOW : row.kind === 'breakeven' ? LEVEL : MUTED }}>{row.label}</span> : null}
                  </td>
                  <td className="px-2 py-1.5 text-right text-secondary-text sm:px-3">{row.movePct == null ? '' : signedPct(row.movePct)}</td>
                  <td className="px-2 py-1.5 text-right font-mono font-semibold sm:px-3" style={{ color: tone(row.pnl) }}>{pnlText(row.pnl)}</td>
                  {basis ? <td className="hidden px-2 py-1.5 text-right font-mono sm:table-cell sm:px-3" style={{ color: tone(row.pnl) }}>{signedPct((row.pnl / basis.amount) * 100)}</td> : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <p className="mt-2 text-[11px] text-muted-text">P/L per position as quoted, at expiration, after fees. Hover the chart for any other price.</p>
    </div>
  );
}
