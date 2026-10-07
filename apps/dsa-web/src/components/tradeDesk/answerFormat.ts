import type { StrategyCandidate, TradeAdviceJob } from '../../types/tradeDesk';
import { formatPercent } from './deskFormat';

/** Notes every candidate of an answer carries (the model's standard caveats), shown once per answer. */
export interface SharedNotes {
  warnings: Set<string>;
  entry: Set<string>;
  exit: Set<string>;
  invalidation: Set<string>;
}

const inEvery = (lists: string[][]): Set<string> => {
  if (lists.length < 2) return new Set();
  const [first, ...rest] = lists;
  return new Set(first.filter((item) => rest.every((list) => list.includes(item))));
};

export const sharedNotes = (candidates: StrategyCandidate[]): SharedNotes => ({
  warnings: inEvery(candidates.map((item) => item.warnings || [])),
  entry: inEvery(candidates.map((item) => item.entryConditions || [])),
  exit: inEvery(candidates.map((item) => item.exitConditions || [])),
  invalidation: inEvery(candidates.map((item) => (item.invalidation ? [item.invalidation] : []))),
});

/** Candidates the model wrote its own take on (reasoning, levels, plan). */
export const reviewedIds = (job: TradeAdviceJob): Set<string> => new Set(Object.keys(job.triggers || {}));

/** Candidates the model picked: the ones it reviewed, unless its verdict is to wait. */
export const pickedIds = (job: TradeAdviceJob): Set<string> => (job.assessment === 'wait' ? new Set() : reviewedIds(job));

/** The candidate to open first: the model's first pick, else the first it reviewed, else the first. */
export const defaultCandidateId = (job: TradeAdviceJob): string | null => {
  const picked = pickedIds(job);
  const reviewed = reviewedIds(job);
  return job.candidates.find((item) => picked.has(item.id))?.id
    ?? job.candidates.find((item) => reviewed.has(item.id))?.id ?? job.candidates[0]?.id ?? null;
};

/** Dollars with thousands separators: $6,629.00. */
export const money = (value: number | null | undefined): string => (
  value == null || !Number.isFinite(Number(value)) ? '—'
    : Number(value).toLocaleString('en-US', { style: 'currency', currency: 'USD' })
);

export const costText = (candidate: StrategyCandidate): string => {
  const debit = candidate.payoff.entryDebit;
  if (debit == null || !Number.isFinite(debit)) return '—';
  return debit < 0 ? `${money(Math.abs(debit))} credit` : `${money(debit)} debit`;
};

export const boundText = (kind: string, value: number | null | undefined): string => (
  kind === 'unbounded' ? 'Unlimited' : value == null || !Number.isFinite(value) ? '—' : money(value)
);

export const breakevenText = (candidate: StrategyCandidate): string => {
  const levels = (candidate.payoff.breakevens || []).filter((level) => Number.isFinite(level) && level > 0);
  return levels.length ? levels.map((level) => level.toFixed(2)).join(' / ') : 'None';
};

export const popText = (candidate: StrategyCandidate): string => (
  candidate.probability.available ? formatPercent(candidate.probability.probabilityOfProfit) : 'n/a'
);

export const expiryOf = (candidate: StrategyCandidate): string | null => (
  candidate.legs.map((leg) => leg.expiry).filter((value): value is string => Boolean(value)).sort()[0] ?? null
);

export const dayLabel = (value: string | null | undefined): string => {
  if (!value) return '—';
  const date = new Date(value.length <= 10 ? `${value}T12:00:00` : value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString([], { month: 'short', day: 'numeric' });
};

const LABELS: Record<string, string> = {
  distribution: 'Price model',
  riskFreeRate: 'Risk-free rate',
  dividendYield: 'Dividend yield',
  initialSpot: 'Price at the quote',
  meanIv: 'Implied volatility',
  perLegIv: 'Implied volatility by leg',
  timeToHorizonYears: 'Time to horizon',
  realWorldSuccessProbability: 'Real-world probability',
  entryFeesOnly: 'Entry fees only',
  anticipatedCloseFees: 'Closing fees assumed',
  closeFeesIncluded: 'Closing fees included',
  pricingModel: 'Pricing',
};

/** "meanIv" -> "Implied volatility"; unknown keys are split into words. */
export const humanKey = (key: string): string => LABELS[key]
  ?? key.replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/^./, (first) => first.toUpperCase());

export const humanValue = (key: string, value: unknown): string => {
  if (value == null || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (key === 'timeToHorizonYears' && typeof value === 'number') {
    const days = value * 365;
    return days < 1 ? `${(days * 24).toFixed(1)} hours` : `${days.toFixed(1)} days`;
  }
  if (/iv$|Iv$|Rate$|Yield$/.test(key)) {
    const values = Array.isArray(value) ? value : [value];
    return values.map((item) => (typeof item === 'number' ? formatPercent(item) : String(item))).join(' / ');
  }
  if (/spot|price/i.test(key) && typeof value === 'number') return money(value);
  if (typeof value === 'string') return value.replace(/_/g, ' ');
  if (Array.isArray(value)) return value.map((item) => String(item)).join(', ');
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(4);
  return JSON.stringify(value);
};

/** Scenario kinds the payoff chart and probability already show. */
export const REDUNDANT_SCENARIOS = new Set(['expiration_payoff', 'expiration_probability']);
