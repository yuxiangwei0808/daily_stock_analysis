import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BookOpen, Check, CircleDollarSign, FileQuestion, Play, RefreshCw, Settings2, ShieldCheck, Sparkles, Trash2, X } from 'lucide-react';
import { Link, useSearchParams } from 'react-router-dom';
import { decisionSignalsApi } from '../api/decisionSignals';
import { tradeDeskApi } from '../api/tradeDesk';
import { AppPage, Badge, Button, Card, ConfirmDialog, EmptyState, InlineAlert, Loading, PageHeader } from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { CreateTradePlanRequest, HoldingsView, StrategyCandidate, TradeAdviceJob, TradeAdviceRequest, TradeDeskCatalogItem, TradeDeskDataMode, TradeDeskHealth, TradeDeskPlan, TradeFillIntent, TradeJournalEvent, UpdateTradePlanRequest, TradeOutcomes, TradePosition, TradePreferences } from '../types/tradeDesk';
import type { DecisionSignalItem } from '../types/decisionSignals';
import { HoldingsPanel } from '../components/tradeDesk/HoldingsPanel';
import { TrackRecordCard } from '../components/tradeDesk/TrackRecordCard';
import { AdviceForm } from '../components/tradeDesk/AdviceForm';
import { NxUsed, PositionUsed, ModeBadge, AdviceVerdict, ModelPanel } from '../components/tradeDesk/AnswerParts';
import { CandidateCard } from '../components/tradeDesk/CandidateCard';
import { PositionCard } from '../components/tradeDesk/PositionCard';
import { QuestionList } from '../components/tradeDesk/QuestionList';
import { ARCHIVE_REASONS, DEFAULT_FORM, parseNumber, parseQuantity, parseInteger, formatMoney, localDateTimeValue, formatDate, formatPercent, heldSummary, liveIsBlocked, errorMessage, statusVariant, statusLabel, textValue } from '../components/tradeDesk/deskFormat';
import type { AdviceFormState, ManualFillState } from '../components/tradeDesk/deskFormat';

type TradeDeskView = 'opportunities' | 'holdings' | 'positions' | 'journal';
const TRADE_DESK_VIEWS: TradeDeskView[] = ['opportunities', 'holdings', 'positions', 'journal'];
const DEFAULT_PREFERENCES: TradePreferences = {
  discordEnabled: false,
};

const TradeDeskPage: React.FC = () => {
  const { t } = useUiLanguage();
  const [searchParams, setSearchParams] = useSearchParams();
  const deepLinkTicker = searchParams.get('ticker')?.trim().toUpperCase() || '';
  const linkedAdviceId = searchParams.get('adviceId');
  const linkedPlanId = searchParams.get('planId');
  const focusedPlanId = useRef<string | null>(null);
  const sourceReportIdRaw = searchParams.get('sourceReportId');
  const sourceReportId = sourceReportIdRaw && /^\d+$/.test(sourceReportIdRaw) ? Number(sourceReportIdRaw) : undefined;
  const [view, setView] = useState<TradeDeskView>(() => {
    if (linkedPlanId) return 'positions';
    const requested = searchParams.get('view');
    if (requested === 'ask') return 'opportunities';
    return TRADE_DESK_VIEWS.includes(requested as TradeDeskView) ? requested as TradeDeskView : 'opportunities';
  });
  const [health, setHealth] = useState<TradeDeskHealth | null>(null);
  const [catalog, setCatalog] = useState<TradeDeskCatalogItem[]>([]);
  const [advice, setAdvice] = useState<TradeAdviceJob[]>([]);
  const [selectedAdviceId, setSelectedAdviceId] = useState<string | null>(linkedAdviceId);
  // Expired, stale, empty or week-old jobs: kept on the server, loaded only when the archive is opened.
  const [archive, setArchive] = useState<TradeAdviceJob[] | null>(null);
  // Answers opened directly (a link, or a selection that left the Current list, e.g. just archived as stale).
  const [pinned, setPinned] = useState<Record<string, TradeAdviceJob>>({});
  const selectedRef = useRef<string | null>(linkedAdviceId);
  const archiveLoadedRef = useRef(false);
  const [adviceScope, setAdviceScope] = useState<'active' | 'archive'>('active');
  const [adviceCounts, setAdviceCounts] = useState<{ active: number; archive: number } | null>(null);
  const [pendingDelete, setPendingDelete] = useState<{ job: TradeAdviceJob } | { archive: number } | null>(null);
  // Broker holdings (read-only) for the "Use my position" hint; absent when no account is set.
  const [heldView, setHeldView] = useState<HoldingsView | null>(null);
  const loadHoldings = useCallback(() => {
    tradeDeskApi.getHoldings().then((result) => setHeldView(result.view)).catch(() => undefined);
  }, []);
  const [plans, setPlans] = useState<TradeDeskPlan[]>([]);
  const [positions, setPositions] = useState<TradePosition[]>([]);
  const [journal, setJournal] = useState<TradeJournalEvent[]>([]);
  const [outcomes, setOutcomes] = useState<TradeOutcomes | null>(null);
  const [preferences, setPreferences] = useState<TradePreferences>(DEFAULT_PREFERENCES);
  const [form, setForm] = useState<AdviceFormState>(() => ({ ...DEFAULT_FORM, ticker: deepLinkTicker }));
  const [selectedStrategies, setSelectedStrategies] = useState<string[]>([]);
  const [followUpForm, setFollowUpForm] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [busyAdviceId, setBusyAdviceId] = useState<string | null>(null);
  const [busyPlanId, setBusyPlanId] = useState<string | null>(null);
  const [manualFillPosition, setManualFillPosition] = useState<TradePosition | null>(null);
  const [paperFillPosition, setPaperFillPosition] = useState<TradePosition | null>(null);
  const [paperFillIntent, setPaperFillIntent] = useState<'open' | 'close'>('open');
  const [paperFillQuantity, setPaperFillQuantity] = useState('1');
  const [paperFillLimit, setPaperFillLimit] = useState('');
  const [manualFill, setManualFill] = useState<ManualFillState>({ contractId: '', side: 'buy', quantity: '1', price: '', fees: '0', intent: 'open', note: '', filledAt: localDateTimeValue() });
  const [reconcilePosition, setReconcilePosition] = useState<TradePosition | null>(null);
  const [reconcileNotes, setReconcileNotes] = useState('');
  const [settlePosition, setSettlePosition] = useState<TradePosition | null>(null);
  const [settlePrice, setSettlePrice] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const lastEventId = useRef('');
  // Incremented by each list request and each locally created job so an
  // older list response cannot drop a newer job or move the selection.
  const adviceListSequence = useRef(0);
  const sseRefreshTimer = useRef<number | null>(null);

  useEffect(() => { document.title = 'Trade Desk - DSA'; }, []);
  // Apply a ?ticker= deep link once; clearing the field afterwards must not refill it.
  const appliedDeepLinkTicker = useRef('');
  useEffect(() => {
    if (!deepLinkTicker || appliedDeepLinkTicker.current === deepLinkTicker) return;
    appliedDeepLinkTicker.current = deepLinkTicker;
    setForm((current) => (current.ticker ? current : { ...current, ticker: deepLinkTicker }));
  }, [deepLinkTicker]);

  const refreshLedger = useCallback(async (includePreferences = false) => {
    // Preferences are an editable form; background refreshes must not revert unsaved edits.
    const results = await Promise.allSettled([tradeDeskApi.listPlans(), tradeDeskApi.listPositions(), tradeDeskApi.listJournal(), tradeDeskApi.getOutcomes(), ...(includePreferences ? [tradeDeskApi.getPreferences()] : [])]);
    if (results[0].status === 'fulfilled') setPlans(results[0].value.items || []);
    if (results[1].status === 'fulfilled') setPositions(results[1].value.items || []);
    if (results[2].status === 'fulfilled') setJournal(results[2].value.items || []);
    if (results[3].status === 'fulfilled') setOutcomes(results[3].value);
    if (results[4]?.status === 'fulfilled') setPreferences(results[4].value as TradePreferences);
    const rejected = results.find((result): result is PromiseRejectedResult => result.status === 'rejected');
    if (rejected) setError(errorMessage(rejected.reason));
  }, []);

  // react-router gives setSearchParams a new identity on every URL change; a ref keeps refreshData stable.
  const setSearchParamsRef = useRef(setSearchParams);
  useEffect(() => { setSearchParamsRef.current = setSearchParams; }, [setSearchParams]);
  const dropLinkedAdvice = useCallback(() => {
    setSearchParamsRef.current((current) => { const next = new URLSearchParams(current); next.delete('adviceId'); return next; }, { replace: true });
  }, []);

  const refreshData = useCallback(async (showSpinner = true) => {
    if (showSpinner) { setIsLoading(true); setError(''); }
    const sequence = ++adviceListSequence.current;
    const withArchive = archiveLoadedRef.current;
    const results = await Promise.allSettled([tradeDeskApi.getHealth(), tradeDeskApi.getCatalog(), tradeDeskApi.listAdvice(),
      withArchive ? tradeDeskApi.listAdvice('archive') : Promise.resolve(null)]);
    if (results[0].status === 'fulfilled') setHealth(results[0].value);
    if (results[1].status === 'fulfilled') setCatalog(results[1].value.items || []);
    if (results[2].status === 'fulfilled' && sequence === adviceListSequence.current) {
      const items = results[2].value.items || [];
      const archived = results[3].status === 'fulfilled' && results[3].value ? results[3].value.items || [] : null;
      const listed = new Set([...items, ...(archived || [])].map((item) => item.id));
      // The linked answer and the one being read stay open even when they are in neither list.
      const extra: Record<string, TradeAdviceJob> = {};
      let linkedGone = false;
      for (const id of new Set([linkedAdviceId, selectedRef.current].filter((value): value is string => Boolean(value)))) {
        if (listed.has(id)) continue;
        try { extra[id] = await tradeDeskApi.getAdvice(id); }
        catch (linkError) {
          if (id === linkedAdviceId) linkedGone = true;
          const status = (linkError as { response?: { status?: number } })?.response?.status;
          if (status !== 404) setError(errorMessage(linkError));
        }
      }
      if (sequence === adviceListSequence.current) {  // a delete, cancel or newer list wins
        setAdvice(items);
        if (archived) setArchive(archived);
        setPinned(extra);
        setAdviceCounts(results[2].value.counts ?? null);
        const available = new Set([...listed, ...Object.keys(extra)]);
        setSelectedAdviceId((current) => current && available.has(current) ? current
          : (linkedAdviceId && available.has(linkedAdviceId) ? linkedAdviceId : items[0]?.id || null));
        if (linkedGone) dropLinkedAdvice();
      }
    }
    const rejected = results.find((result): result is PromiseRejectedResult => result.status === 'rejected');
    if (rejected) setError(errorMessage(rejected.reason));
    await refreshLedger(showSpinner);
    if (showSpinner) setIsLoading(false);
  }, [refreshLedger, linkedAdviceId, dropLinkedAdvice]);

  useEffect(() => { void refreshData(); }, [refreshData]);
  // Holdings behind "You hold …", the held badges and ticker suggestions: fresh whenever the ask tab opens.
  useEffect(() => { if (view === 'opportunities') loadHoldings(); }, [view, loadHoldings]);
  useEffect(() => { setSearchParams((current) => { const next = new URLSearchParams(current); next.set('view', view); return next; }, { replace: true }); }, [setSearchParams, view]);

  useEffect(() => {
    if (!health?.enabled || typeof EventSource === 'undefined') return undefined;
    const source = new EventSource(tradeDeskApi.getEventsUrl(lastEventId.current || undefined), { withCredentials: true });
    source.onmessage = (event) => {
      lastEventId.current = event.lastEventId || lastEventId.current;
      // Discord delivery bookkeeping (two rows per message sent) changes nothing on this page.
      try {
        const type = (JSON.parse(event.data) as { event_type?: string }).event_type;
        if (type === 'discord_attempt' || type === 'discord_delivery') return;
      } catch { /* not JSON: refresh as before */ }
      if (sseRefreshTimer.current == null) {
        sseRefreshTimer.current = window.setTimeout(() => {
          sseRefreshTimer.current = null;
          void refreshData(false);
        }, 500);
      }
    };
    source.onerror = () => { /* EventSource reconnects and sends Last-Event-ID. */ };
    return () => {
      source.close();
      if (sseRefreshTimer.current != null) {
        window.clearTimeout(sseRefreshTimer.current);
        sseRefreshTimer.current = null;
      }
    };
  }, [health?.enabled, refreshData]);

  useEffect(() => {
    if (linkedPlanId) setView('positions');
    else if (linkedAdviceId) { setSelectedAdviceId(linkedAdviceId); setView('opportunities'); }
  }, [linkedAdviceId, linkedPlanId]);

  useEffect(() => {
    if (view !== 'positions' || isLoading || !linkedPlanId || focusedPlanId.current === linkedPlanId) return;
    const target = document.getElementById(`trade-plan-${linkedPlanId}`);
    if (target) {
      target.scrollIntoView?.({ block: 'center' });
      target.focus({ preventScroll: true });
      focusedPlanId.current = linkedPlanId;
    }
  }, [view, isLoading, linkedPlanId, positions]);

  const activeAdviceIds = advice.filter((item) => item.status === 'queued' || item.status === 'running').map((item) => item.id).join(',');
  useEffect(() => {
    if (!activeAdviceIds) return undefined;
    const interval = window.setInterval(() => { void Promise.all(activeAdviceIds.split(',').map(async (id) => { try { const current = await tradeDeskApi.getAdvice(id); setAdvice((items) => items.map((item) => item.id === id ? current : item)); } catch { /* SSE and the next interval retry. */ } })); }, 2000);
    return () => window.clearInterval(interval);
  }, [activeAdviceIds]);

  useEffect(() => {
    if (view !== 'positions') return undefined;
    const interval = window.setInterval(() => {
      void tradeDeskApi.listPositions().then((result) => setPositions(result.items || [])).catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(interval);
  }, [view]);

  const selectedAdvice = useMemo(() => advice.find((item) => item.id === selectedAdviceId)
    || archive?.find((item) => item.id === selectedAdviceId) || (selectedAdviceId ? pinned[selectedAdviceId] : undefined) || null,
  [advice, archive, pinned, selectedAdviceId]);
  useEffect(() => { selectedRef.current = selectedAdviceId; setFollowUpForm(''); }, [selectedAdviceId]);
  const heldNote = heldSummary(heldView, form.ticker);
  // The newest active report verdict for the typed ticker (US), shown in the ask panel.
  const [reportVerdict, setReportVerdict] = useState<{ ticker: string; item: DecisionSignalItem | null } | null>(null);
  const verdictTicker = form.ticker.trim().toUpperCase();
  useEffect(() => {
    if (!/^[A-Z][A-Z0-9.-]{0,9}$/.test(verdictTicker)) return undefined;
    let active = true;
    const timer = window.setTimeout(() => {
      decisionSignalsApi.getLatest(verdictTicker, { market: 'us', limit: 1 })
        .then((result) => { if (active) setReportVerdict({ ticker: verdictTicker, item: result.items[0] ?? null }); })
        .catch(() => { if (active) setReportVerdict({ ticker: verdictTicker, item: null }); });
    }, 400);
    return () => { active = false; window.clearTimeout(timer); };
  }, [verdictTicker]);
  const currentVerdict = reportVerdict?.ticker === verdictTicker ? reportVerdict.item : null;
  const heldTickers = useMemo(() => new Set([...(heldView?.stocks || []).map((row) => row.ticker),
    ...(heldView?.options || []).filter((row) => !row.expired).map((row) => row.underlying)]), [heldView]);
  // Ticker suggestions: what you hold, then what you asked about recently.
  const tickerOptions = useMemo(() => Array.from(new Set([...heldTickers, ...advice.map((job) => job.request.ticker)])).slice(0, 40), [heldTickers, advice]);
  const [askOpen, setAskOpen] = useState<boolean | null>(null);
  const askPanelOpen = askOpen ?? (Boolean(deepLinkTicker) || advice.length === 0);
  // From a held position: open the ask panel on that ticker with your position attached.
  const askAbout = (ticker: string) => {
    loadHoldings();
    setForm((current) => ({ ...current, ticker, dataMode: 'live', useHoldings: true }));
    setAskOpen(true);
    setView('opportunities');
    if (typeof window !== 'undefined') window.scrollTo?.({ top: 0, behavior: 'smooth' });
  };
  const selectQuestion = (job: TradeAdviceJob) => {
    setSelectedAdviceId(job.id);
    if (typeof window !== 'undefined' && window.innerWidth < 1024) {
      window.setTimeout(() => document.getElementById('advice-detail')?.scrollIntoView?.({ block: 'start', behavior: 'smooth' }), 0);
    }
  };
  const showScope = async (scope: 'active' | 'archive') => {
    setAdviceScope(scope);
    if (scope !== 'archive') return;
    archiveLoadedRef.current = true;  // later refreshes keep the archive current too
    const sequence = ++adviceListSequence.current;
    try {
      const result = await tradeDeskApi.listAdvice('archive');
      if (sequence !== adviceListSequence.current) return;
      setArchive(result.items || []);
      if (result.counts) setAdviceCounts(result.counts);
    } catch (archiveError) { setError(errorMessage(archiveError)); }
  };
  const intradayCandidates = selectedAdvice?.candidates.filter((candidate) => candidate.horizon === 'intraday') || [];
  const swingCandidates = selectedAdvice?.candidates.filter((candidate) => candidate.horizon === 'swing') || [];

  const adviceRequest = (): TradeAdviceRequest => ({
    ticker: form.ticker.trim().toUpperCase(),
    allocation: parseNumber(form.allocation),
    direction: form.direction,
    horizon: form.horizon,
    dataMode: form.dataMode,
    expiry: form.expiry || undefined,
    strategies: selectedStrategies,
    message: form.message.trim(),
    sourceReportId,
    existingShares: parseInteger(form.existingShares),
    feePerContract: parseNumber(form.feePerContract) ?? 0.65,
    riskFreeRate: parseNumber(form.riskFreeRate) ?? 0,
    dividendYield: parseNumber(form.dividendYield) ?? 0,
    marginPerUnit: parseNumber(form.marginPerUnit),
    planLegs: form.planLegs.trim() || undefined,
    useHoldings: form.useHoldings,
  });

  const submitAdvice = async () => {
    setError(''); setMessage('');
    if (!form.ticker.trim()) return;
    if (form.dataMode === 'live' && liveIsBlocked(health)) { setError(health?.live.message || t('tradeDesk.liveUnavailable')); return; }
    setIsSubmitting(true);
    try { const job = await tradeDeskApi.createAdvice(adviceRequest()); adviceListSequence.current += 1; setAdvice((items) => [job, ...items.filter((item) => item.id !== job.id)]); setSelectedAdviceId(job.id); setAskOpen(false); setMessage(`Asked about ${job.request.ticker}; the answer appears under Your questions.`); } catch (submitError) { setError(errorMessage(submitError)); } finally { setIsSubmitting(false); }
  };

  const cancelAdvice = async (job: TradeAdviceJob) => {
    setBusyAdviceId(job.id); setError('');
    try { const updated = await tradeDeskApi.cancelAdvice(job.id); adviceListSequence.current += 1; setAdvice((items) => items.map((item) => item.id === job.id ? updated : item)); } catch (cancelError) { setError(errorMessage(cancelError)); } finally { setBusyAdviceId(null); }
  };

  const removeAdvice = (ids: string[]) => {
    const gone = new Set(ids);
    adviceListSequence.current += 1;  // a list request already in flight must not bring these back
    setPinned((items) => Object.fromEntries(Object.entries(items).filter(([id]) => !gone.has(id))));
    if (linkedAdviceId && gone.has(linkedAdviceId)) dropLinkedAdvice();
    const activeGone = advice.filter((item) => gone.has(item.id)).length;
    const archiveGone = ids.length - activeGone;
    setAdvice((items) => items.filter((item) => !gone.has(item.id)));
    setArchive((items) => items && items.filter((item) => !gone.has(item.id)));
    setAdviceCounts((counts) => counts && { active: Math.max(0, counts.active - activeGone), archive: Math.max(0, counts.archive - archiveGone) });
    setSelectedAdviceId((current) => current && gone.has(current) ? null : current);
  };

  const confirmDelete = async () => {
    const target = pendingDelete;
    setPendingDelete(null);
    if (!target) return;
    setError(''); setMessage('');
    try {
      if ('job' in target) {
        setBusyAdviceId(target.job.id);
        const result = await tradeDeskApi.deleteAdvice(target.job.id);
        removeAdvice(result.deleted);
        setMessage(`Deleted the ${target.job.request.ticker} request`);
      } else {
        const result = await tradeDeskApi.deleteArchivedAdvice();
        removeAdvice(result.deleted);
        const kept = Object.keys(result.kept || {}).length;
        setMessage(`Deleted ${result.deleted.length} archived requests${kept ? `; kept ${kept} linked to a plan or still running` : ''}`);
      }
    } catch (deleteError) { setError(errorMessage(deleteError)); } finally { setBusyAdviceId(null); }
  };

  const followUp = async (job: TradeAdviceJob, overrideMessage?: string) => {
    const content = (overrideMessage ?? followUpForm).trim();
    if (!content) return;
    setIsSubmitting(true); setError('');
    const baseline = { ...job.request };
    const effective = job.candidates.map((candidate) => job.effectiveRequests?.[candidate.id] || job.request);
    const fields: (keyof TradeAdviceRequest)[] = ['allocation', 'direction', 'horizon', 'expiry', 'strategies', 'existingShares', 'feePerContract', 'riskFreeRate', 'dividendYield', 'marginPerUnit'];
    for (const field of fields) {
      if (effective.length && effective.every((request) => JSON.stringify(request[field]) === JSON.stringify(effective[0][field]))) {
        Object.assign(baseline, { [field]: effective[0][field] });
      }
    }
    try { const child = await tradeDeskApi.createAdvice({ ...baseline, message: content, parentAdviceId: job.id }); adviceListSequence.current += 1; setAdvice((items) => [child, ...items]); setSelectedAdviceId(child.id); setFollowUpForm(''); } catch (followError) { setError(errorMessage(followError)); } finally { setIsSubmitting(false); }
  };

  const monitorCandidate = async (request: CreateTradePlanRequest) => {
    setBusyPlanId(request.candidateId); setError('');
    try { const plan = await tradeDeskApi.createPlan(request); setPlans((items) => [plan, ...items.filter((item) => item.id !== plan.id)]); setMessage(t('tradeDesk.monitoring')); } catch (monitorError) { setError(errorMessage(monitorError)); } finally { setBusyPlanId(null); }
  };

  const openPaperFill = (position: TradePosition) => { setPaperFillPosition(position); setPaperFillIntent('open'); setPaperFillQuantity('1'); setPaperFillLimit(''); };
  const submitPaperFill = async () => {
    if (!paperFillPosition) return;
    setBusyPlanId(paperFillPosition.planId); setError('');
    const quantity = parseQuantity(paperFillQuantity);
    if (quantity == null) { setError('Enter a whole-number quantity of at least 1.'); setBusyPlanId(null); return; }
    try { await tradeDeskApi.paperFill(paperFillPosition.planId, { intent: paperFillIntent, quantity, limitPrice: parseNumber(paperFillLimit) }); setPaperFillPosition(null); await refreshLedger(); setMessage(t('tradeDesk.paperFill')); } catch (fillError) { setError(errorMessage(fillError)); } finally { setBusyPlanId(null); }
  };

  // Every field is reset per plan so a previous dialog's intent, quantity or
  // note is never carried into another plan. Held legs default to closing.
  const manualFillDefaults = (position: TradePosition, contractId: string): Pick<ManualFillState, 'side' | 'intent' | 'price'> => {
    const plan = plans.find((item) => item.id === position.planId) || position.plan;
    const held = position.legs.find((leg) => leg.contractId === contractId);
    const opening = plan?.candidate.legs.find((leg) => leg.contractId === contractId);
    const mark = held?.markPrice;
    if (held && held.signedQuantity && position.status !== 'watching') {
      return { side: held.signedQuantity > 0 ? 'sell' : 'buy', intent: 'close', price: mark == null ? '' : String(mark) };
    }
    return { side: opening?.side === 'sell' ? 'sell' : 'buy', intent: 'open', price: mark == null ? '' : String(mark) };
  };
  const openManualFill = (position: TradePosition) => {
    const plan = plans.find((item) => item.id === position.planId) || position.plan;
    const heldLeg = position.status !== 'watching' ? position.legs.find((leg) => !(leg.right === 'stock' && plan?.candidate.legs.some((item) => item.existing && item.contractId === leg.contractId))) : undefined;
    const contractId = heldLeg?.contractId || plan?.candidate.legs.find((leg) => !leg.existing)?.contractId || plan?.candidate.underlying || '';
    setManualFillPosition(position);
    setManualFill({ contractId, quantity: '1', fees: '0', note: '', filledAt: localDateTimeValue(), ...manualFillDefaults(position, contractId) });
  };
  const submitManualFill = async () => {
    if (!manualFillPosition || !manualFill.contractId || parseNumber(manualFill.price) == null) return;
    const quantity = parseQuantity(manualFill.quantity);
    if (quantity == null) { setError('Enter a whole-number quantity of at least 1.'); return; }
    if (!manualFill.filledAt || Number.isNaN(new Date(manualFill.filledAt).getTime())) { setError('Enter the actual fill time.'); return; }
    setBusyPlanId(manualFillPosition.planId); setError('');
    try { await tradeDeskApi.createFill(manualFillPosition.planId, { contractId: manualFill.contractId, side: manualFill.side, quantity, price: parseNumber(manualFill.price) || 0, fees: parseNumber(manualFill.fees) || 0, filledAt: new Date(manualFill.filledAt).toISOString(), intent: manualFill.intent, note: manualFill.note }); setManualFillPosition(null); await refreshLedger(); } catch (fillError) { setError(errorMessage(fillError)); } finally { setBusyPlanId(null); }
  };
  const submitReconcile = async () => {
    if (!reconcilePosition || !reconcileNotes.trim()) return;
    setBusyPlanId(reconcilePosition.planId); setError('');
    try { await tradeDeskApi.reconcilePlan(reconcilePosition.planId, { notes: reconcileNotes.trim() }); setReconcilePosition(null); setReconcileNotes(''); await refreshLedger(); } catch (reconcileError) { setError(errorMessage(reconcileError)); } finally { setBusyPlanId(null); }
  };
  const submitSettle = async () => {
    const price = parseNumber(settlePrice);
    if (!settlePosition || price == null || price <= 0) { setError('Enter the underlying price at expiration.'); return; }
    setBusyPlanId(settlePosition.planId); setError('');
    try { await tradeDeskApi.paperSettle(settlePosition.planId, price); setSettlePosition(null); setSettlePrice(''); await refreshLedger(); } catch (settleError) { setError(errorMessage(settleError)); } finally { setBusyPlanId(null); }
  };
  const updatePlan = async (planId: string, changes: UpdateTradePlanRequest) => {
    setBusyPlanId(planId); setError('');
    try { const plan = await tradeDeskApi.updatePlan(planId, changes); setPlans((items) => items.map((item) => item.id === plan.id ? plan : item)); await refreshLedger(); } catch (planError) { setError(errorMessage(planError)); } finally { setBusyPlanId(null); }
  };
  const savePreferences = async () => {
    setError('');
    try { setPreferences(await tradeDeskApi.updatePreferences(preferences)); setMessage(t('tradeDesk.savePreferences')); } catch (preferenceError) { setError(errorMessage(preferenceError)); }
  };

  const setActiveView = (nextView: TradeDeskView) => { setView(nextView); };
  // Arrow keys, Home and End move between tabs (WAI-ARIA tabs pattern).
  const onTabKey = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    const index = TRADE_DESK_VIEWS.indexOf(view);
    const next = event.key === 'ArrowRight' ? (index + 1) % TRADE_DESK_VIEWS.length
      : event.key === 'ArrowLeft' ? (index - 1 + TRADE_DESK_VIEWS.length) % TRADE_DESK_VIEWS.length
        : event.key === 'Home' ? 0 : event.key === 'End' ? TRADE_DESK_VIEWS.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    setActiveView(TRADE_DESK_VIEWS[next]);
    document.getElementById(`trade-desk-tab-${TRADE_DESK_VIEWS[next]}`)?.focus();
  };
  const listedAdvice = adviceScope === 'archive' ? archive || [] : advice;
  const renderQuestions = () => <Card variant="bordered" padding="sm" className="lg:sticky lg:top-4">
    <div className="mb-2 flex items-center justify-between gap-2 px-1">
      <h2 className="whitespace-nowrap text-sm font-semibold text-foreground">{t('tradeDesk.yourQuestions')}</h2>
      <div className="flex shrink-0 gap-1 whitespace-nowrap text-xs" role="group" aria-label="Question history">{(['active', 'archive'] as const).map((scope) => <button key={scope} type="button" aria-pressed={adviceScope === scope} onClick={() => void showScope(scope)} className={`rounded-lg px-2 py-1 ${adviceScope === scope ? 'bg-cyan/10 text-cyan' : 'text-secondary-text hover:text-foreground'}`}>{scope === 'active' ? 'Current' : 'Archive'} ({scope === 'active' ? adviceCounts?.active ?? advice.length : adviceCounts?.archive ?? archive?.length ?? 0})</button>)}</div>
    </div>
    {adviceScope === 'archive' ? <div className="mb-2 flex flex-wrap items-center justify-between gap-2 px-1"><p className="text-xs text-secondary-text">Expired, stale, empty or week-old questions, kept until you delete them.</p>{archive?.length ? <Button size="xsm" variant="ghost" onClick={() => setPendingDelete({ archive: adviceCounts?.archive ?? archive.length })}><Trash2 className="h-3.5 w-3.5" />Delete all archived</Button> : null}</div> : null}
    {listedAdvice.length
      ? <QuestionList key={adviceScope} jobs={listedAdvice} selectedId={selectedAdviceId} heldTickers={heldTickers} busyId={busyAdviceId} onSelect={selectQuestion} onCancel={(job) => void cancelAdvice(job)} onDelete={(job) => setPendingDelete({ job })} />
      : <p className="px-1 py-6 text-center text-xs text-secondary-text">{adviceScope === 'archive' ? 'Nothing archived.' : 'No questions yet. Ask about a stock above.'}</p>}
  </Card>;
  const candidateList = (items: StrategyCandidate[], job: TradeAdviceJob) => <div className="space-y-4">{items.map((candidate) => <CandidateCard key={candidate.id} candidate={candidate} advice={job} onMonitor={monitorCandidate} monitoring={busyPlanId === candidate.id || plans.some((plan) => plan.adviceId === job.id && (plan.candidateId === candidate.id || plan.candidate.id === candidate.id))} />)}</div>;
  const renderAnswer = () => {
    if (!selectedAdvice) return <Card variant="bordered" padding="md"><EmptyState icon={<FileQuestion className="h-8 w-8" />} title={t('tradeDesk.yourQuestions')} description={t('tradeDesk.pickQuestion')} /></Card>;
    const job = selectedAdvice;
    const running = job.status === 'queued' || job.status === 'running';
    return <Card variant="bordered" padding="md"><div id="advice-detail" className="scroll-mt-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2"><h2 className="font-mono text-lg font-semibold text-foreground">{job.request.ticker}</h2><ModeBadge mode={job.request.dataMode} /><Badge variant={statusVariant(job.status)}>{statusLabel(job.status)}</Badge>{job.parentAdviceId ? <Badge variant="history">{t('tradeDesk.followUpBadge')}</Badge> : null}{job.archived ? <Badge variant="default">{ARCHIVE_REASONS[job.archived] || job.archived}</Badge> : null}</div>
          {job.request.message ? <p className="mt-1 text-sm text-secondary-text">“{job.request.message}”</p> : null}
          <p className="mt-1 text-xs text-muted-text" title={`snapshot ${textValue(job.snapshot && (job.snapshot as { id?: unknown }).id) || 'pending'}`}>Updated {formatDate(job.updatedAt)}</p>
        </div>
        <Button size="sm" variant="ghost" onClick={() => void refreshData(false)}><RefreshCw className="h-3.5 w-3.5" />{t('tradeDesk.refresh')}</Button>
      </div>
      {running ? <p className="mt-4 flex items-center gap-2 text-sm text-secondary-text"><RefreshCw className="h-4 w-4 animate-spin text-cyan" />Working on it; answers usually take one to three minutes.</p> : null}
      {job.error ? <InlineAlert className="mt-4" variant="danger" title={t('tradeDesk.jobError')} message={job.error} /> : null}
      {job.request.dataMode === 'replay' ? <InlineAlert className="mt-4" variant="info" message={t('tradeDesk.replaySynthetic')} /> : null}
      {job.planError ? <InlineAlert className="mt-3" variant="warning" title={t('tradeDesk.planNotPriced')} message={job.planError} /> : null}
      <PositionUsed job={job} />
      <NxUsed job={job} />
      {job.panel?.opinions?.length ? <ModelPanel panel={job.panel} /> : null}
      {job.explanation ? <AdviceVerdict job={job} /> : null}
      {job.status === 'stale' ? <InlineAlert className="mt-3" variant="warning" message={t('tradeDesk.staleAdvice')} action={<Button size="sm" variant="outline" isLoading={isSubmitting} onClick={() => void followUp(job, job.request.message || t('tradeDesk.runAgain'))}>{t('tradeDesk.runAgain')}</Button>} /> : null}
      <div className="mt-5 space-y-5">
        {intradayCandidates.length ? <section><h3 className="mb-3 text-sm font-semibold text-foreground">{t('tradeDesk.intraday')}</h3>{candidateList(intradayCandidates, job)}</section> : null}
        {swingCandidates.length ? <section><h3 className="mb-3 text-sm font-semibold text-foreground">{t('tradeDesk.swing')}</h3>{candidateList(swingCandidates, job)}</section> : null}
        {job.status === 'completed' && job.candidates.length === 0 && !job.explanation ? <EmptyState title={t('tradeDesk.noCandidates')} description={t('tradeDesk.description')} /> : null}
      </div>
      {!running ? <div className="mt-5 border-t border-border/50 pt-4">
        <textarea aria-label={t('tradeDesk.followUp')} value={followUpForm} onChange={(event) => setFollowUpForm(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && followUpForm.trim() && !isSubmitting) void followUp(job); }} rows={2} placeholder={t('tradeDesk.followUpPlaceholder')} className="input-surface w-full rounded-xl border px-3 py-2 text-sm text-foreground" />
        <div className="mt-2 flex justify-end"><Button size="sm" variant="outline" disabled={!followUpForm.trim()} isLoading={isSubmitting} onClick={() => void followUp(job)}><Sparkles className="h-4 w-4" />{t('tradeDesk.followUp')}</Button></div>
      </div> : null}
    </div></Card>;
  };
  const renderOpportunities = () => <div className="space-y-5"><AdviceForm form={form} setForm={setForm} catalog={catalog} health={health} selectedStrategies={selectedStrategies} setSelectedStrategies={setSelectedStrategies} onSubmit={() => void submitAdvice()} isSubmitting={isSubmitting} sourceReportId={sourceReportId} heldNote={heldNote} reportVerdict={currentVerdict} open={askPanelOpen} onToggle={() => setAskOpen(!askPanelOpen)} tickerOptions={tickerOptions} /><div className="grid gap-5 lg:grid-cols-[340px_minmax(0,1fr)] lg:items-start">{renderQuestions()}{renderAnswer()}</div></div>;
  const manualFillContracts = manualFillPosition ? (() => {
    const plan = plans.find((item) => item.id === manualFillPosition.planId) || manualFillPosition.plan;
    const candidateContracts = plan?.candidate.legs.map((leg) => leg.contractId) || [];
    const heldContracts = manualFillPosition.legs.map((leg) => leg.contractId);
    return Array.from(new Set([...heldContracts, ...candidateContracts, plan?.candidate.underlying].filter((item): item is string => Boolean(item))));
  })() : [];
  const renderPositions = () => <div className="space-y-5"><InlineAlert variant="info" message={t('tradeDesk.noLiveOrders')} />{paperFillPosition ? <Card variant="gradient" padding="md"><div className="flex items-center justify-between"><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.paperFill')} · {paperFillPosition.underlying}</h2><Button size="sm" variant="ghost" onClick={() => setPaperFillPosition(null)}><X className="h-4 w-4" />{t('tradeDesk.close')}</Button></div><div className="mt-4 grid gap-3 md:grid-cols-3"><label className="text-xs text-secondary-text">{t('tradeDesk.intent')}<select value={paperFillIntent} onChange={(event) => setPaperFillIntent(event.target.value as 'open' | 'close')} className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground"><option value="open">{t('tradeDesk.open')}</option><option value="close">{t('tradeDesk.closeIntent')}</option></select></label><label className="text-xs text-secondary-text">{t('tradeDesk.quantity')}<input value={paperFillQuantity} onChange={(event) => setPaperFillQuantity(event.target.value)} type="number" min="1" step="1" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label><label className="text-xs text-secondary-text">Net debit limit (whole fill, USD; negative = minimum credit)<input value={paperFillLimit} onChange={(event) => setPaperFillLimit(event.target.value)} type="number" step="any" placeholder="optional" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label></div><Button className="mt-3" onClick={() => void submitPaperFill()} isLoading={busyPlanId === paperFillPosition.planId} disabled={!paperFillQuantity.trim()}><Play className="h-4 w-4" />{t('tradeDesk.paperFill')}</Button></Card> : null}{plans.length ? <Card variant="bordered" padding="md"><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.monitoring')}</h2><div className="mt-3 grid gap-2 md:grid-cols-2">{plans.map((plan) => <div key={plan.id} className="rounded-xl border border-border/50 p-3 text-sm"><div className="flex items-center justify-between gap-2"><span className="font-mono text-foreground">{plan.candidate.underlying}</span><div className="flex gap-1"><ModeBadge mode={plan.dataMode} /><Badge variant={plan.ledger === 'paper' ? 'info' : 'warning'}>{plan.ledger}</Badge></div></div><p className="mt-1 text-xs text-secondary-text">{plan.candidate.title} · {plan.status} · {plan.monitoring ? t('tradeDesk.monitoring') : 'paused'}</p></div>)}</div></Card> : null}{positions.length ? <div className="space-y-4">{positions.map((position) => <div key={position.planId} id={`trade-plan-${position.planId}`} tabIndex={-1}><PositionCard position={position} plans={plans} onPaperFill={openPaperFill} onManualFill={openManualFill} onUpdatePlan={(planId, changes) => void updatePlan(planId, changes)} onSettle={(item) => { setSettlePosition(item); setSettlePrice(''); }} onReconcile={(item) => setReconcilePosition(item)} /></div>)}</div> : <EmptyState icon={<ShieldCheck className="h-8 w-8" />} title={t('tradeDesk.noPositions')} description={t('tradeDesk.noLiveOrders')} />}{manualFillPosition ? <Card variant="gradient" padding="md"><div className="flex items-center justify-between"><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.manualFill')} · {manualFillPosition.underlying}</h2><Button size="sm" variant="ghost" onClick={() => setManualFillPosition(null)}><X className="h-4 w-4" />{t('tradeDesk.close')}</Button></div><div className="mt-4 grid gap-3 md:grid-cols-3"><label className="text-xs text-secondary-text">{t('tradeDesk.contract')}<select value={manualFill.contractId} onChange={(event) => { const contractId = event.target.value; setManualFill((current) => ({ ...current, contractId, ...(manualFillPosition ? manualFillDefaults(manualFillPosition, contractId) : {}) })); }} className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground">{manualFillContracts.map((contractId) => <option key={contractId} value={contractId}>{contractId}</option>)}</select></label><label className="text-xs text-secondary-text">{t('tradeDesk.side')}<select value={manualFill.side} onChange={(event) => setManualFill((current) => ({ ...current, side: event.target.value as 'buy' | 'sell' }))} className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground"><option value="buy">buy</option><option value="sell">sell</option></select></label><label className="text-xs text-secondary-text">{t('tradeDesk.quantity')}<input value={manualFill.quantity} onChange={(event) => setManualFill((current) => ({ ...current, quantity: event.target.value }))} type="number" min="1" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label><label className="text-xs text-secondary-text">{t('tradeDesk.price')}<input value={manualFill.price} onChange={(event) => setManualFill((current) => ({ ...current, price: event.target.value }))} type="number" step="any" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label><label className="text-xs text-secondary-text">{t('tradeDesk.fees')}<input value={manualFill.fees} onChange={(event) => setManualFill((current) => ({ ...current, fees: event.target.value }))} type="number" min="0" step="any" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label><label className="text-xs text-secondary-text">{t('tradeDesk.intent')}<select value={manualFill.intent} onChange={(event) => setManualFill((current) => ({ ...current, intent: event.target.value as TradeFillIntent }))} className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground"><option value="open">{t('tradeDesk.open')}</option><option value="close">{t('tradeDesk.closeIntent')}</option><option value="assignment">assignment</option><option value="exercise">exercise</option></select></label><label className="text-xs text-secondary-text">Filled at<input aria-label="Filled at" type="datetime-local" value={manualFill.filledAt} onChange={(event) => setManualFill((current) => ({ ...current, filledAt: event.target.value }))} className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label></div><label className="mt-3 block text-xs text-secondary-text">{t('tradeDesk.note')}<textarea value={manualFill.note} onChange={(event) => setManualFill((current) => ({ ...current, note: event.target.value }))} rows={2} className="input-surface mt-1 w-full rounded-lg border px-3 py-2 text-sm text-foreground" /></label><Button className="mt-3" onClick={() => void submitManualFill()} isLoading={busyPlanId === manualFillPosition.planId} disabled={!manualFill.contractId || parseNumber(manualFill.price) == null}><CircleDollarSign className="h-4 w-4" />{t('tradeDesk.submitFill')}</Button></Card> : null}{settlePosition ? <Card variant="gradient" padding="md"><div className="flex items-center justify-between"><h2 className="text-lg font-semibold text-foreground">Settle expiry · {settlePosition.underlying}</h2><Button size="sm" variant="ghost" onClick={() => setSettlePosition(null)}><X className="h-4 w-4" />{t('tradeDesk.close')}</Button></div><p className="mt-2 text-xs text-secondary-text">Expired paper option legs close at intrinsic value for this underlying price. Share delivery is not simulated.</p><label className="mt-3 block text-xs text-secondary-text">Underlying price at expiration (USD)<input aria-label="Underlying price at expiration" value={settlePrice} onChange={(event) => setSettlePrice(event.target.value)} type="number" min="0" step="any" className="input-surface mt-1 h-10 w-full rounded-lg border px-2 text-sm text-foreground" /></label><Button className="mt-3" onClick={() => void submitSettle()} isLoading={busyPlanId === settlePosition.planId} disabled={!(parseNumber(settlePrice) && (parseNumber(settlePrice) ?? 0) > 0)}><Check className="h-4 w-4" />Settle expiry</Button></Card> : null}{reconcilePosition ? <Card variant="gradient" padding="md"><div className="flex items-center justify-between"><h2 className="text-lg font-semibold text-foreground">Reconcile {reconcilePosition.underlying}</h2><Button size="sm" variant="ghost" onClick={() => setReconcilePosition(null)}><X className="h-4 w-4" />{t('tradeDesk.close')}</Button></div><textarea value={reconcileNotes} onChange={(event) => setReconcileNotes(event.target.value)} rows={3} placeholder="Record broker assignment/exercise details" className="input-surface mt-3 w-full rounded-xl border px-3 py-2 text-sm text-foreground" /><Button className="mt-3" onClick={() => void submitReconcile()} isLoading={busyPlanId === reconcilePosition.planId} disabled={!reconcileNotes.trim()}><Check className="h-4 w-4" />Reconcile</Button></Card> : null}</div>;
  // Replay events must never read as live: resolve each event's data mode from
  // its payload, plan or advice so the journal can label it.
  const journalMode = (event: TradeJournalEvent): string | undefined => {
    const payload = event.payload || {};
    if (typeof payload.dataMode === 'string') return payload.dataMode;
    const planId = event.planId || (typeof payload.planId === 'string' ? payload.planId : undefined);
    const plan = planId ? plans.find((item) => item.id === planId) : undefined;
    if (plan) return plan.dataMode;
    const adviceId = event.adviceId || (typeof payload.adviceId === 'string' ? payload.adviceId : undefined);
    return adviceId ? advice.find((item) => item.id === adviceId)?.request.dataMode : undefined;
  };
  const renderJournal = () => <div className="space-y-5"><TrackRecordCard /><div className="grid gap-4 md:grid-cols-2"><Card variant="bordered" padding="md"><div className="flex items-center gap-2"><BookOpen className="h-5 w-5 text-cyan" /><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.outcomes')}</h2></div><div className="mt-4 grid gap-4 sm:grid-cols-2">{(['paper', 'paperReplay', 'manualLive'] as const).map((ledger) => { const bucket = outcomes?.[ledger]; return <div key={ledger} className="rounded-xl bg-elevated/50 p-3"><div className="flex items-center justify-between gap-2"><span className="flex flex-wrap items-center gap-2 text-sm font-semibold text-foreground">{ledger === 'manualLive' ? t('tradeDesk.manualLive') : t('tradeDesk.paper')}{ledger === 'paperReplay' ? <ModeBadge mode="replay" /> : null}</span><Badge variant={ledger === 'paper' ? 'info' : 'warning'}>{bucket?.closedTrades ?? 0}</Badge></div><p className="mt-2 text-sm text-secondary-text">{t('tradeDesk.realizedPnl')}: <strong className="text-foreground">{formatMoney(bucket?.realizedPnl)}</strong></p><p className="mt-1 text-xs text-secondary-text">{t('tradeDesk.winRate')}: {formatPercent(bucket?.winRate)}</p>{typeof bucket?.note === 'string' ? <p className="mt-1 text-xs text-muted-text">{bucket.note}</p> : null}</div>; })}</div></Card><Card variant="bordered" padding="md"><div className="flex items-center gap-2"><Settings2 className="h-5 w-5 text-cyan" /><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.preferences')}</h2></div><div className="mt-4 space-y-3 text-sm"><label className="flex items-center justify-between gap-3"><span>{t('tradeDesk.discord')}</span><input type="checkbox" checked={preferences.discordEnabled} onChange={(event) => setPreferences((current) => ({ ...current, discordEnabled: event.target.checked }))} /></label><div className="flex flex-wrap items-center justify-between gap-2"><Button size="sm" onClick={() => void savePreferences()}>{t('tradeDesk.savePreferences')}</Button><Link className="text-xs text-cyan hover:underline" to="/settings">{t('tradeDesk.discordSettings')}</Link></div></div></Card></div>{journal.length ? <Card variant="bordered" padding="md"><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.journal')}</h2><div className="mt-3 divide-y divide-border/40">{journal.map((event) => <div key={String(event.id)} className="grid min-w-0 gap-2 py-3 text-sm md:grid-cols-[150px_minmax(0,1fr)_180px]"><div className="flex min-w-0 flex-wrap items-start gap-2 font-medium text-foreground [overflow-wrap:anywhere]">{event.eventType}{journalMode(event) ? <ModeBadge mode={journalMode(event) as TradeDeskDataMode} /> : null}</div><div className="min-w-0 text-secondary-text [overflow-wrap:anywhere]">{Object.entries(event.payload || {}).map(([key, value]) => <span key={key} className="mr-3 inline-block max-w-full"><span className="text-muted-text">{key}</span>: {textValue(value)}</span>)}</div><div className="text-xs text-muted-text">{formatDate(event.createdAt)}</div></div>)}</div></Card> : <EmptyState icon={<BookOpen className="h-8 w-8" />} title={t('tradeDesk.noJournal')} description={t('tradeDesk.description')} />}</div>;

  if (isLoading && !health) return <AppPage><Loading label={t('common.loading')} /></AppPage>;
  if (health && !health.enabled) return <AppPage><InlineAlert variant="warning" title={t('tradeDesk.unavailable')} message="Trade Desk is disabled by the server configuration." /></AppPage>;
  return <AppPage><PageHeader eyebrow={t('tradeDesk.eyebrow')} title={t('tradeDesk.title')} description={t('tradeDesk.description')} actions={<><Button size="sm" variant="ghost" onClick={() => void refreshData()}><RefreshCw className="h-4 w-4" />{t('tradeDesk.refresh')}</Button><Link to="/settings" className="inline-flex h-9 items-center gap-2 rounded-lg border border-border/60 px-3 text-sm text-secondary-text hover:text-foreground"><Settings2 className="h-4 w-4" />Settings</Link></>} /><div className="mt-4 flex flex-wrap gap-2 rounded-2xl border border-border/50 bg-card/50 p-2" role="tablist" aria-label={t('tradeDesk.title')}>{([['opportunities', t('tradeDesk.ask')], ['holdings', t('tradeDesk.holdingsTab')], ['positions', t('tradeDesk.positions')], ['journal', t('tradeDesk.journal')]] as const).map(([key, label]) => <button key={key} id={`trade-desk-tab-${key}`} type="button" role="tab" aria-selected={view === key} aria-controls="trade-desk-panel" tabIndex={view === key ? 0 : -1} onKeyDown={onTabKey} onClick={() => setActiveView(key)} className={`rounded-xl px-4 py-2 text-sm transition ${view === key ? 'bg-cyan/10 text-cyan' : 'text-secondary-text hover:text-foreground'}`}>{label}</button>)}</div>{error ? <InlineAlert className="mt-4" variant="danger" title={t('common.failure')} message={error} action={<Button size="sm" variant="ghost" onClick={() => setError('')}>{t('common.close')}</Button>} /> : null}{message ? <InlineAlert className="mt-4" variant="success" message={message} /> : null}<ConfirmDialog isOpen={pendingDelete !== null} isDanger title={pendingDelete && 'job' in pendingDelete ? `Delete the ${pendingDelete.job.request.ticker} request?` : 'Delete all archived requests?'} message={pendingDelete && 'job' in pendingDelete ? 'The request and its answer are removed permanently. The journal keeps its log entries.' : `${pendingDelete && 'archive' in pendingDelete ? pendingDelete.archive : 0} archived requests are removed permanently. Requests linked to a monitored plan are kept.`} confirmText="Delete" onConfirm={() => void confirmDelete()} onCancel={() => setPendingDelete(null)} /><div className="mt-5" id="trade-desk-panel" role="tabpanel" aria-labelledby={`trade-desk-tab-${view}`}>{view === 'opportunities' ? renderOpportunities() : view === 'holdings' ? <HoldingsPanel onAsk={askAbout} /> : view === 'positions' ? renderPositions() : renderJournal()}</div></AppPage>;
};

export default TradeDeskPage;
