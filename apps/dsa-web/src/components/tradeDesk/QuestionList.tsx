import { useMemo, useState } from 'react';
import { ChevronRight, Pause, Trash2 } from 'lucide-react';
import { Badge, Button } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { TradeAdviceJob } from '../../types/tradeDesk';
import { ModeBadge } from './AnswerParts';
import { ARCHIVE_REASONS, statusVariant, statusLabel, shortDate } from './deskFormat';

export function QuestionList({ jobs, selectedId, heldTickers, busyId, onSelect, onCancel, onDelete }: {
  jobs: TradeAdviceJob[];
  selectedId: string | null;
  heldTickers: Set<string>;
  busyId: string | null;
  onSelect: (job: TradeAdviceJob) => void;
  onCancel: (job: TradeAdviceJob) => void;
  onDelete: (job: TradeAdviceJob) => void;
}) {
  const { t } = useUiLanguage();
  const [filter, setFilter] = useState('');
  const [toggled, setToggled] = useState<Record<string, boolean>>({});
  const groups = useMemo(() => {
    const byTicker = new Map<string, TradeAdviceJob[]>();
    for (const job of jobs) {
      const ticker = job.request.ticker;
      byTicker.set(ticker, [...(byTicker.get(ticker) || []), job]);
    }
    return Array.from(byTicker.entries());
  }, [jobs]);
  const needle = filter.trim().toUpperCase();
  const shown = needle ? groups.filter(([ticker]) => ticker.includes(needle)) : groups;
  const isOpen = (ticker: string, items: TradeAdviceJob[], index: number) => toggled[ticker] ?? (
    index === 0 || Boolean(needle) || items.some((job) => job.id === selectedId));
  return (
    <div className="space-y-2">
      {groups.length > 3 || filter ? <input aria-label={t('tradeDesk.filterTickers')} value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t('tradeDesk.filterTickers')} className="input-surface h-9 w-full rounded-lg border px-3 text-xs text-foreground" /> : null}
      {needle && !shown.length ? <p className="px-1 py-3 text-center text-xs text-secondary-text">No ticker matches “{filter}”.</p> : null}
      {shown.map(([ticker, items], index) => {
        const open = isOpen(ticker, items, index);
        const latest = items[0];
        return (
          <section key={ticker} className="rounded-xl border border-border/50 bg-card/40" data-testid={`question-group-${ticker}`}>
            <button type="button" aria-expanded={open} onClick={() => setToggled((current) => ({ ...current, [ticker]: !open }))} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left">
              <span className="flex min-w-0 items-center gap-2">
                <ChevronRight className={`h-3.5 w-3.5 shrink-0 text-secondary-text transition-transform ${open ? 'rotate-90' : ''}`} />
                <strong className="font-mono text-sm text-foreground">{ticker}</strong>
                <span className="text-xs text-secondary-text">{items.length}</span>
                {heldTickers.has(ticker) ? <Badge variant="info">{t('tradeDesk.held')}</Badge> : null}
              </span>
              <Badge variant={statusVariant(latest.status)}>{statusLabel(latest.status)}</Badge>
            </button>
            {open ? <ul className="space-y-1 border-t border-border/40 p-1.5">
              {items.map((job) => {
                const selected = job.id === selectedId;
                const running = job.status === 'queued' || job.status === 'running';
                return (
                  <li key={job.id}>
                    <div className={`flex items-start justify-between gap-2 rounded-lg px-2 py-1.5 ${selected ? 'bg-cyan/10 ring-1 ring-cyan/30' : 'hover:bg-hover'}`}>
                      <button type="button" aria-current={selected || undefined} onClick={() => onSelect(job)} className="min-w-0 flex-1 text-left">
                        <span className="block truncate text-sm text-foreground" title={job.request.message || undefined}>{job.request.message || 'Compare strategies'}</span>
                        <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-secondary-text">
                          <span>{shortDate(job.createdAt)}</span>
                          <Badge variant={statusVariant(job.status)}>{statusLabel(job.status)}</Badge>
                          {job.request.dataMode === 'replay' ? <ModeBadge mode="replay" /> : null}
                          {job.candidates.length ? <span>{job.candidates.length} ideas</span> : null}
                          {job.parentAdviceId ? <Badge variant="history">{t('tradeDesk.followUpBadge')}</Badge> : null}
                          {job.archived ? <Badge variant="default">{ARCHIVE_REASONS[job.archived] || job.archived}</Badge> : null}
                        </span>
                      </button>
                      {running
                        ? <Button size="xsm" variant="ghost" isLoading={busyId === job.id} onClick={() => onCancel(job)}><Pause className="h-3.5 w-3.5" />{t('tradeDesk.cancelJob')}</Button>
                        : <Button size="xsm" variant="ghost" aria-label={`Delete ${ticker} request from ${shortDate(job.createdAt)}`} title="Delete" isLoading={busyId === job.id} onClick={() => onDelete(job)}><Trash2 className="h-3.5 w-3.5" /></Button>}
                    </div>
                  </li>
                );
              })}
            </ul> : null}
          </section>
        );
      })}
    </div>
  );
}
