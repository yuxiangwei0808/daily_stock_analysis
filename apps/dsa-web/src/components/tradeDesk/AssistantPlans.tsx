import { useState } from 'react';
import { Trash2 } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Badge, Button } from '../common';
import type { AssistantPlan, AssistantPlanDraft, AssistantTrigger } from '../../types/tradeDesk';
import { price } from './chartOverlays';
import { errorMessage } from './deskFormat';

const TRIGGERS: Array<[AssistantTrigger, string]> = [['now', 'Now'], ['above', 'Trades at or above'], ['below', 'Trades at or below'],
  ['close_above', 'A bar closes above'], ['close_below', 'A bar closes below']];
const STATUS: Record<string, string> = { waiting: 'Waiting', open: 'In the trade', closed: 'Graded', void: 'Void', expired: 'Expired' };
const EMPTY: AssistantPlanDraft = { direction: 'long', trigger: { kind: 'now', price: null, barMinutes: null }, invalidation: null,
  stop: null, target: null, timeStop: null, instrument: null };
const text = (value: number | null) => (value == null ? '' : String(value));
const number = (value: string) => (value.trim() === '' ? null : Number(value));

function describe(plan: AssistantPlan) {
  const trigger = plan.trigger.kind === 'now' ? 'from the price then'
    : `${TRIGGERS.find(([key]) => key === plan.trigger.kind)?.[1].toLowerCase()} ${price(plan.trigger.price || 0)}${plan.trigger.barMinutes ? ` (${plan.trigger.barMinutes}-min bar)` : ''}`;
  return `${plan.direction === 'long' ? 'Long' : 'Short'} ${trigger}, stop ${price(plan.stop)}, target ${price(plan.target)}, out by ${plan.timeStop}`;
}

/** Your plans on one stock: type it or fill it in, confirm, and the session watches it; graded after the close. */
export function AssistantPlans({ ticker, plans }: { ticker: string; plans: AssistantPlan[] }) {
  const [words, setWords] = useState('');
  const [draft, setDraft] = useState<AssistantPlanDraft | null>(null);
  const [questions, setQuestions] = useState<string[]>([]);
  const [model, setModel] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  const read = async () => {
    if (!words.trim()) { setError('Type your plan first'); return; }
    setBusy('read'); setError('');
    try {
      const out = await tradeDeskApi.parseAssistantPlan(ticker, words);
      setDraft({ ...EMPTY, ...out.draft, trigger: { ...EMPTY.trigger, ...out.draft.trigger } });
      setQuestions(out.questions); setModel(out.model);
    } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(''); }
  };
  const watch = async () => {
    if (!draft || !draft.direction || draft.stop == null || draft.target == null) { setError('A plan needs a direction, a stop and a target'); return; }
    setBusy('watch'); setError('');
    try {
      await tradeDeskApi.addAssistantPlan({ ticker, direction: draft.direction, triggerKind: draft.trigger.kind, triggerPrice: draft.trigger.price,
        barMinutes: draft.trigger.kind.startsWith('close_') ? (draft.trigger.barMinutes || 5) : null, invalidation: draft.invalidation,
        stop: draft.stop, target: draft.target, timeStop: draft.timeStop || null, instrument: draft.instrument, text: words });
      setDraft(null); setWords(''); setQuestions([]); setModel('');
    } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(''); }
  };
  const remove = async (id: string) => {
    try { await tradeDeskApi.removeAssistantPlan(id); } catch (failure) { setError(errorMessage(failure)); }
  };
  const set = (patch: Partial<AssistantPlanDraft>) => setDraft((current) => (current ? { ...current, ...patch } : current));
  const field = 'input-surface h-8 rounded-md border px-2 font-mono text-foreground';

  return (
    <div data-testid="assistant-plans">
      <h4 className="pb-1 text-xs font-semibold text-foreground">Your plan</h4>
      <div className="flex flex-wrap items-start gap-2 text-xs">
        <textarea aria-label="Your plan in words" value={words} onChange={(event) => setWords(event.target.value)} rows={2}
          placeholder={`For example: calls if a 5-minute bar closes above VWAP, stop under the prior-day low, target ${ticker === 'SPY' ? '742' : 'the prior-day high'}, out by 3:30`}
          className="input-surface min-w-[16rem] flex-1 rounded-md border px-2 py-1 text-foreground" />
        <div className="flex flex-col gap-1">
          <Button size="xsm" variant="outline" isLoading={busy === 'read'} onClick={() => void read()}>Read it</Button>
          <Button size="xsm" variant="ghost" onClick={() => { setDraft({ ...EMPTY }); setQuestions([]); setModel(''); }}>Fill in by hand</Button>
        </div>
      </div>
      {draft ? <div className="mt-2 space-y-2 rounded-lg border border-border/40 p-2 text-xs">
        {model ? <p className="text-muted-text">Read by {model}. Check every field: nothing is watched until you confirm.</p> : null}
        {questions.length ? <ul className="list-disc pl-4 text-warning">{questions.map((q) => <li key={q}>{q}</li>)}</ul> : null}
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-0.5 text-secondary-text">Direction
            <select aria-label="Plan direction" value={draft.direction || 'long'} onChange={(event) => set({ direction: event.target.value as 'long' | 'short' })} className={field}>
              <option value="long">Long (calls, shares)</option><option value="short">Short (puts)</option>
            </select></label>
          <label className="flex flex-col gap-0.5 text-secondary-text">Enter when
            <select aria-label="Plan trigger" value={draft.trigger.kind} onChange={(event) => set({ trigger: { ...draft.trigger, kind: event.target.value as AssistantTrigger } })} className={field}>
              {TRIGGERS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select></label>
          {draft.trigger.kind !== 'now' ? <label className="flex flex-col gap-0.5 text-secondary-text">Price
            <input aria-label="Plan trigger price" inputMode="decimal" value={text(draft.trigger.price)} className={`${field} w-24`}
              onChange={(event) => set({ trigger: { ...draft.trigger, price: number(event.target.value) } })} /></label> : null}
          {draft.trigger.kind.startsWith('close_') ? <label className="flex flex-col gap-0.5 text-secondary-text">Bar
            <select aria-label="Plan bar size" value={draft.trigger.barMinutes || 5} className={field}
              onChange={(event) => set({ trigger: { ...draft.trigger, barMinutes: Number(event.target.value) as 1 | 5 | 15 } })}>
              <option value={1}>1 min</option><option value={5}>5 min</option><option value={15}>15 min</option>
            </select></label> : null}
          <label className="flex flex-col gap-0.5 text-secondary-text">Stop
            <input aria-label="Plan stop" inputMode="decimal" value={text(draft.stop)} onChange={(event) => set({ stop: number(event.target.value) })} className={`${field} w-24`} /></label>
          <label className="flex flex-col gap-0.5 text-secondary-text">Target
            <input aria-label="Plan target" inputMode="decimal" value={text(draft.target)} onChange={(event) => set({ target: number(event.target.value) })} className={`${field} w-24`} /></label>
          <label className="flex flex-col gap-0.5 text-secondary-text">Cancel if it trades
            <input aria-label="Plan invalidation" inputMode="decimal" value={text(draft.invalidation)} onChange={(event) => set({ invalidation: number(event.target.value) })} className={`${field} w-24`} /></label>
          <label className="flex flex-col gap-0.5 text-secondary-text">Out by (ET)
            <input aria-label="Plan time stop" value={draft.timeStop || ''} placeholder="close" onChange={(event) => set({ timeStop: event.target.value || null })} className={`${field} w-20`} /></label>
          <Button size="xsm" variant="outline" isLoading={busy === 'watch'} onClick={() => void watch()}>Watch this plan</Button>
          <Button size="xsm" variant="ghost" onClick={() => setDraft(null)}>Cancel</Button>
        </div>
      </div> : null}
      {error ? <p role="alert" className="pt-1 text-xs text-danger">{error}</p> : null}
      {plans.length ? <ul className="mt-2 space-y-1 text-xs">{plans.map((plan) => <li key={plan.id} className="flex items-start justify-between gap-2 border-t border-border/30 pt-1">
        <div className="min-w-0 text-secondary-text">
          <Badge variant={plan.status === 'open' ? 'warning' : 'default'}>{STATUS[plan.status] || plan.status}</Badge>{' '}
          {plan.source === 'assistant' ? <span className="text-muted-text">Assistant call (untested model call) · </span> : null}{describe(plan)}
          {plan.price != null && plan.status !== 'waiting' ? ` · in at ${price(plan.price)}` : ''}
          {plan.liveExit && plan.status === 'open' ? ` · ${plan.liveExit.how} at ${price(plan.liveExit.price)}` : ''}
          {plan.status === 'closed' && plan.r != null ? ` · ${plan.how} ${plan.r >= 0 ? '+' : '−'}${Math.abs(plan.r).toFixed(2)}R${plan.twinR != null ? `, random timing ${plan.twinR >= 0 ? '+' : '−'}${Math.abs(plan.twinR).toFixed(2)}R` : ''}` : ''}
          {plan.void ? ` · ${plan.void}` : ''}{plan.why ? <span className="block text-muted-text">{plan.why}</span> : null}
        </div>
        {plan.status === 'waiting' ? <button type="button" aria-label="Remove plan" className="shrink-0 text-muted-text hover:text-danger"
          onClick={() => void remove(plan.id)}><Trash2 className="h-3.5 w-3.5" /></button> : null}
      </li>)}</ul> : null}
    </div>
  );
}
