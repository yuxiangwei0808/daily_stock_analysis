import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { BellPlus, Check, MessageSquareText, Trash2 } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Badge, Button, Card, InlineAlert } from '../common';
import type { HoldingRule, HoldingRuleKind, PositionAction, PositionAnswerItem, PositionQuestion } from '../../types/tradeDesk';

const POLL_MS = 3000;
const SHOWN = 5;

export interface PositionChoice {
  key: string;
  ticker: string;
  label: string;
}

export interface PositionAskHandle {
  /** Picks a position and puts the cursor in the question box (the "Ask" on a holding row). */
  focusOn: (key: string) => void;
}

const QUICK: Array<{ text: string; all?: boolean }> = [
  { text: 'Where should my stop-loss be?' },
  { text: 'At what price should I take profit?' },
  { text: 'Hold, trim or close?' },
  { text: 'Review all my positions: what to hold, trim or close, with stops and targets.', all: true },
];

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

function errorText(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  return error instanceof Error ? error.message : 'Request failed';
}

const price = (value?: number | null) => (value == null ? '—' : value.toFixed(2));

/** One suggested level with the button that turns it into an alert (never an order). */
function Level({ title, value, basis, rule, item, rules, onSet, ruleBasis }: {
  title: string; value: string; basis?: string; ruleBasis?: boolean;
  rule: { kind: HoldingRuleKind; value: number } | null; item: PositionAnswerItem; rules: HoldingRule[];
  onSet: (item: PositionAnswerItem, rule: { kind: HoldingRuleKind; value: number }, note: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const set = rule != null && rules.some((existing) => existing.positionKey === item.key && existing.kind === rule.kind
    && Math.abs(existing.value - rule.value) < 0.005 && existing.status !== 'triggered');
  return (
    <div className="min-w-0 rounded-xl bg-elevated/50 px-3 py-2">
      <div className="text-xs text-secondary-text">{title}</div>
      <div className="mt-0.5 font-mono text-sm font-semibold text-foreground">{value}</div>
      {basis ? <div className={`mt-0.5 text-xs [overflow-wrap:anywhere] ${ruleBasis ? 'text-warning' : 'text-muted-text'}`}>{basis}</div> : null}
      {rule ? (
        <div className="mt-1 text-xs">
          {set
            ? <span className="inline-flex items-center gap-1 text-success"><Check className="h-3.5 w-3.5" />Alert set</span>
            : <Button size="xsm" variant="ghost" isLoading={busy} aria-label={`Set ${title.toLowerCase()} alert for ${item.ticker}`}
              onClick={async () => { setBusy(true); try { await onSet(item, rule, `${title} from the desk's answer${basis ? `: ${basis}` : ''}`); } finally { setBusy(false); } }}>
              <BellPlus className="h-3.5 w-3.5" />Set alert
            </Button>}
        </div>
      ) : null}
    </div>
  );
}

function priceRule(item: PositionAnswerItem, level?: number | null): { kind: HoldingRuleKind; value: number } | null {
  if (level == null) return null;
  // An alert fires when the price reaches the level from where it is now.
  const below = item.price != null ? level < item.price : item.exposure !== 'short';
  return { kind: below ? 'price_below' : 'price_above', value: level };
}

function AnswerItem({ item, rules, onSet }: {
  item: PositionAnswerItem; rules: HoldingRule[];
  onSet: (item: PositionAnswerItem, rule: { kind: HoldingRuleKind; value: number }, note: string) => Promise<void>;
}) {
  const action = ACTION[item.action] ?? ACTION.review;
  const on = item.type === 'option' ? `${item.ticker} at` : '';
  return (
    <li className="rounded-xl border border-border/50 p-3" data-testid={`position-answer-${item.key}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground">{item.label}</span>
        <Badge variant={action.variant}>{action.label}</Badge>
        <span className="text-xs text-muted-text">{item.type === 'option' ? `${item.ticker} ` : ''}now {price(item.price)}{item.pnlPct != null ? ` · ${item.pnlPct >= 0 ? '+' : ''}${item.pnlPct.toFixed(1)}% on cost` : ''}</span>
      </div>
      {item.reason ? <p className="mt-2 text-sm leading-6 text-foreground">{item.reason}</p> : null}
      <div className="mt-2 grid grid-cols-2 gap-2 lg:grid-cols-4 text-xs">
        <Level title="Stop" value={item.stop != null ? `${on} ${price(item.stop)}`.trim() : 'None'} basis={item.stopBasis}
          ruleBasis={item.stopSource === 'rule'} rule={priceRule(item, item.stop)} item={item} rules={rules} onSet={onSet} />
        <Level title="Target" value={item.target != null ? `${on} ${price(item.target)}`.trim() : 'None'} basis={item.targetBasis}
          ruleBasis={item.targetSource === 'rule'} rule={priceRule(item, item.target)} item={item} rules={rules} onSet={onSet} />
        {item.pnlStopPct != null ? <Level title="Cut at P&L" value={`${item.pnlStopPct.toFixed(0)}% on cost`}
          rule={{ kind: 'pnl_below', value: item.pnlStopPct }} item={item} rules={rules} onSet={onSet} /> : null}
        {item.pnlTargetPct != null ? <Level title="Take profit at P&L" value={`+${item.pnlTargetPct.toFixed(0)}% on cost`}
          rule={{ kind: 'pnl_above', value: item.pnlTargetPct }} item={item} rules={rules} onSet={onSet} /> : null}
      </div>
      {item.risk ? <p className="mt-2 text-xs leading-5 text-warning">Risk: {item.risk}</p> : null}
    </li>
  );
}

function Answer({ question, rules, choices, onSet, onDelete }: {
  question: PositionQuestion; rules: HoldingRule[]; choices: PositionChoice[];
  onSet: (item: PositionAnswerItem, rule: { kind: HoldingRuleKind; value: number }, note: string) => Promise<void>;
  onDelete: (question: PositionQuestion) => void;
}) {
  const about = question.positionKey ? choices.find((choice) => choice.key === question.positionKey)?.label ?? question.positionKey : 'All positions';
  return (
    <article className="rounded-2xl border border-border/50 bg-card/30 p-3" data-testid="position-question">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-medium text-foreground">{question.question}</p>
          <p className="mt-0.5 text-xs text-muted-text">{about} · {new Date(question.createdAt).toLocaleString()}{question.model ? ` · ${question.model}` : ''}</p>
        </div>
        {question.status !== 'running'
          ? <Button size="xsm" variant="ghost" aria-label="Delete this question" onClick={() => onDelete(question)}><Trash2 className="h-3.5 w-3.5" /></Button>
          : null}
      </div>
      {question.status === 'running' ? <p className="mt-2 animate-pulse text-sm text-secondary-text">Reading your positions and their levels… (about a minute)</p> : null}
      {question.status === 'failed' ? <InlineAlert className="mt-2" variant="danger" message={question.error || 'No answer'} /> : null}
      {question.status === 'done' ? (
        <>
          {question.summary ? <p className="mt-2 whitespace-pre-line text-sm leading-6 text-foreground">{question.summary}</p> : null}
          {question.positions?.length
            ? <ul className="mt-3 space-y-2">{question.positions.map((item) => <AnswerItem key={item.key} item={item} rules={rules} onSet={onSet} />)}</ul>
            : null}
        </>
      ) : null}
    </article>
  );
}

/** Ask the model about what you hold: stops, profit targets, hold or sell. Levels become alerts on a click. */
export const PositionAsk = forwardRef<PositionAskHandle, {
  choices: PositionChoice[]; rules: HoldingRule[]; onRulesChanged: () => void; onCompare?: (ticker: string) => void;
}>(({ choices, rules, onRulesChanged, onCompare }, ref) => {
  const [items, setItems] = useState<PositionQuestion[] | null>(null);
  const [positionKey, setPositionKey] = useState('');
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');
  const [showAll, setShowAll] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const card = useRef<HTMLDivElement>(null);

  useImperativeHandle(ref, () => ({
    focusOn: (key: string) => {
      setPositionKey(key);
      card.current?.scrollIntoView?.({ block: 'start', behavior: 'smooth' });
      window.setTimeout(() => box.current?.focus(), 0);
    },
  }), []);

  const load = useCallback(async () => {
    try {
      setItems(await tradeDeskApi.getPositionQuestions());
    } catch (error) {
      setProblem(errorText(error));
    }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const running = Boolean(items?.some((item) => item.status === 'running'));
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => { void load(); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [running, load]);

  const ask = async (text = question, key = positionKey) => {
    if (!text.trim()) return;
    setBusy(true);
    setProblem('');
    try {
      const created = await tradeDeskApi.askAboutPositions(text.trim(), key || null);
      setItems((current) => [created, ...(current ?? []).filter((item) => item.id !== created.id)]);
      setQuestion('');
    } catch (error) {
      setProblem(errorText(error));
    } finally {
      setBusy(false);
    }
  };
  const setAlert = async (item: PositionAnswerItem, rule: { kind: HoldingRuleKind; value: number }, note: string) => {
    try {
      await tradeDeskApi.createHoldingRule({ positionKey: item.key, ticker: item.ticker, kind: rule.kind, value: rule.value,
        note: note.slice(0, 200), repeat: 'once' });
      onRulesChanged();
    } catch (error) {
      setProblem(errorText(error));
    }
  };
  const remove = async (target: PositionQuestion) => {
    try {
      await tradeDeskApi.deletePositionQuestion(target.id);
      setItems((current) => (current ?? []).filter((item) => item.id !== target.id));
    } catch (error) {
      setProblem(errorText(error));
    }
  };
  const picked = choices.find((choice) => choice.key === positionKey);
  const shown = showAll ? items ?? [] : (items ?? []).slice(0, SHOWN);

  return (
    <div ref={card} className="scroll-mt-4">
      <Card variant="bordered" padding="md">
        <div data-testid="position-ask">
          <h2 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-secondary-text"><MessageSquareText className="h-4 w-4" />Ask about your positions</h2>
          <p className="mt-1 text-xs text-muted-text">Stop-losses, profit targets, hold or sell — answered from your positions, their levels and the latest report. Levels become alerts only when you set them; nothing is traded.</p>
          <div className="mt-3 grid gap-2 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)]">
            <label className="text-xs text-secondary-text">
              Position
              <select aria-label="Position" value={positionKey} onChange={(event) => setPositionKey(event.target.value)}
                className="input-surface mt-1 h-9 w-full rounded-lg border px-2 text-sm text-foreground">
                <option value="">All positions</option>
                {choices.map((choice) => <option key={choice.key} value={choice.key}>{choice.label}</option>)}
              </select>
            </label>
            <label className="text-xs text-secondary-text">
              Question
              <textarea ref={box} aria-label="Question about your positions" rows={2} value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) void ask(); }}
                placeholder="e.g. Where should I set a stop-loss? At what price should I sell?"
                className="input-surface mt-1 w-full rounded-lg border px-2 py-1.5 text-sm text-foreground" />
            </label>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5 text-xs">
            {QUICK.map((quick) => (
              <button key={quick.text} type="button" disabled={busy || running}
                onClick={() => { if (quick.all) setPositionKey(''); void ask(quick.text, quick.all ? '' : positionKey); }}
                className="rounded-full border border-border/60 px-2.5 py-1 text-secondary-text transition hover:border-cyan/50 hover:text-foreground disabled:opacity-50">
                {quick.all ? 'Review all my positions' : quick.text}
              </button>
            ))}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Button size="sm" onClick={() => void ask()} isLoading={busy} disabled={!question.trim() || running}>Ask</Button>
            {running ? <span className="text-xs text-secondary-text">One question at a time: the current one is still being answered.</span> : null}
            {picked && onCompare ? <button type="button" className="text-xs text-cyan hover:underline" onClick={() => onCompare(picked.ticker)}>Compare option strategies for {picked.ticker} →</button> : null}
          </div>
          {problem ? <InlineAlert className="mt-3" variant="danger" message={problem} /> : null}
          {items?.length ? (
            <div className="mt-4 space-y-3">
              {shown.map((item) => <Answer key={item.id} question={item} rules={rules} choices={choices} onSet={setAlert} onDelete={(target) => void remove(target)} />)}
              {items.length > SHOWN ? <button type="button" className="text-xs text-cyan hover:underline" onClick={() => setShowAll((value) => !value)}>{showAll ? 'Show fewer' : `Show all ${items.length}`}</button> : null}
            </div>
          ) : null}
          <p className="mt-3 text-xs text-muted-text">Suggested levels are the model's judgment and rules of thumb, not tested signals.</p>
        </div>
      </Card>
    </div>
  );
});
PositionAsk.displayName = 'PositionAsk';
