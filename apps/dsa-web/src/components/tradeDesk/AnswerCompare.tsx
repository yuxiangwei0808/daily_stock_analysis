import { Info, Star } from 'lucide-react';
import type { StrategyCandidate, TradeAdviceJob } from '../../types/tradeDesk';
import { boundText, breakevenText, costText, dayLabel, expiryOf, pickedIds, popText } from './answerFormat';
import type { SharedNotes } from './answerFormat';

const GAIN = 'hsl(var(--color-success))';
const LOSS = 'hsl(var(--color-danger))';

/** The candidates side by side; a row opens that candidate below. */
export function AnswerCompare({ job, selectedId, onSelect }: {
  job: TradeAdviceJob; selectedId: string | null; onSelect: (candidate: StrategyCandidate) => void;
}) {
  const picked = pickedIds(job);
  const horizons = new Set(job.candidates.map((item) => item.horizon));
  return (
    <section className="mt-5" data-testid="answer-compare">
      <h3 className="mb-2 text-sm font-semibold text-foreground">Compared strategies</h3>
      {/* Wide screens: a table. */}
      <div className="hidden overflow-hidden rounded-xl border border-border/50 md:block">
        <table className="w-full text-left text-sm">
          <thead className="bg-elevated/50 text-xs text-secondary-text">
            <tr>
              <th className="px-3 py-2 font-medium">Strategy</th>
              <th className="px-3 py-2 font-medium">Cost</th>
              <th className="px-3 py-2 font-medium">Max gain</th>
              <th className="px-3 py-2 font-medium">Max loss</th>
              <th className="px-3 py-2 font-medium">Breakeven</th>
              <th className="px-3 py-2 font-medium">Chance of profit</th>
              <th className="px-3 py-2 font-medium">Expires</th>
            </tr>
          </thead>
          <tbody>
            {job.candidates.map((candidate) => {
              const selected = candidate.id === selectedId;
              return (
                <tr key={candidate.id} aria-selected={selected} onClick={() => onSelect(candidate)}
                  className={`cursor-pointer border-t border-border/40 transition ${selected ? 'bg-cyan/10' : 'hover:bg-hover/60'}`}>
                  <td className="px-3 py-2.5">
                    <button type="button" onClick={(event) => { event.stopPropagation(); onSelect(candidate); }}
                      className={`text-left font-medium ${selected ? 'text-cyan' : 'text-foreground'}`}>
                      {picked.has(candidate.id) ? <Star className="mr-1 inline h-3.5 w-3.5 fill-current text-cyan" aria-label="Picked by the desk" /> : null}
                      {candidate.title.replace(/ \((intraday|swing)\)$/, '')}
                    </button>
                    {horizons.size > 1 ? <span className="ml-2 text-xs text-muted-text">{candidate.horizon}</span> : null}
                  </td>
                  <td className="px-3 py-2.5 font-mono text-foreground">{costText(candidate)}</td>
                  <td className="px-3 py-2.5 font-mono" style={{ color: GAIN }}>{boundText(candidate.payoff.gainBound, candidate.payoff.maxGain)}</td>
                  <td className="px-3 py-2.5 font-mono" style={{ color: LOSS }}>{boundText(candidate.payoff.lossBound, candidate.payoff.maxLoss)}</td>
                  <td className="px-3 py-2.5 font-mono text-foreground">{breakevenText(candidate)}</td>
                  <td className="px-3 py-2.5 font-mono text-foreground">{popText(candidate)}</td>
                  <td className="px-3 py-2.5 text-secondary-text">{dayLabel(expiryOf(candidate))}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {/* Phones: one compact card per candidate. */}
      <div className="grid gap-2 text-xs md:hidden">
        {job.candidates.map((candidate) => {
          const selected = candidate.id === selectedId;
          return (
            <button key={candidate.id} type="button" aria-pressed={selected} onClick={() => onSelect(candidate)}
              className={`rounded-xl border p-3 text-left text-xs ${selected ? 'border-cyan/50 bg-cyan/10' : 'border-border/50'}`}>
              <span className="flex items-center gap-1 text-sm font-medium text-foreground">
                {picked.has(candidate.id) ? <Star className="h-3.5 w-3.5 fill-current text-cyan" aria-label="Picked by the desk" /> : null}
                {candidate.title.replace(/ \((intraday|swing)\)$/, '')}
              </span>
              <span className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 text-secondary-text">
                <span>Cost <strong className="text-foreground">{costText(candidate)}</strong></span>
                <span>Chance <strong className="text-foreground">{popText(candidate)}</strong></span>
                <span>Max gain <strong style={{ color: GAIN }}>{boundText(candidate.payoff.gainBound, candidate.payoff.maxGain)}</strong></span>
                <span>Max loss <strong style={{ color: LOSS }}>{boundText(candidate.payoff.lossBound, candidate.payoff.maxLoss)}</strong></span>
                <span>Breakeven <strong className="text-foreground">{breakevenText(candidate)}</strong></span>
                <span>Expires <strong className="text-foreground">{dayLabel(expiryOf(candidate))}</strong></span>
              </span>
            </button>
          );
        })}
      </div>
      {picked.size ? <p className="mt-2 text-xs text-muted-text"><Star className="mr-1 inline h-3 w-3 fill-current text-cyan" />Picked by the desk</p> : null}
    </section>
  );
}

/** The caveats every candidate shares, said once instead of on each card. */
export function SharedNotesBox({ notes }: { notes: SharedNotes }) {
  const items = [...notes.warnings, ...notes.entry, ...notes.exit, ...notes.invalidation];
  if (!items.length) return null;
  return (
    <details className="mt-3 rounded-xl border border-border/40 bg-card/20 px-3 py-2 text-xs text-secondary-text" data-testid="shared-notes">
      <summary className="cursor-pointer select-none text-secondary-text"><Info className="mr-1 inline h-3.5 w-3.5" />About these numbers ({items.length} notes that apply to every strategy)</summary>
      <ul className="mt-2 space-y-1 leading-5">{items.map((item) => <li key={item}>• {item}</li>)}</ul>
    </details>
  );
}
