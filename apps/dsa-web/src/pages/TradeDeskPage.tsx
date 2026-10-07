import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BookOpen, FileQuestion, RefreshCw, Settings2, Sparkles, Trash2 } from 'lucide-react';
import { Link, useSearchParams } from 'react-router-dom';
import { decisionSignalsApi } from '../api/decisionSignals';
import { tradeDeskApi } from '../api/tradeDesk';
import { AppPage, Badge, Button, Card, ConfirmDialog, EmptyState, InlineAlert, Loading, PageHeader } from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { HoldingsView, TradeAdviceJob, TradeAdviceRequest, TradeDeskCatalogItem, TradeDeskDataMode, TradeDeskHealth, TradeJournalEvent, TradePreferences } from '../types/tradeDesk';
import type { DecisionSignalItem } from '../types/decisionSignals';
import { HoldingsPanel } from '../components/tradeDesk/HoldingsPanel';
import { TrackRecordCard } from '../components/tradeDesk/TrackRecordCard';
import { TradeJournalCard } from '../components/tradeDesk/TradeJournalCard';
import { AdviceForm } from '../components/tradeDesk/AdviceForm';
import { NxUsed, ReferencesUsed, PositionUsed, ModeBadge, AdviceVerdict, ModelPanel } from '../components/tradeDesk/AnswerParts';
import { DiscordPreferences } from '../components/tradeDesk/DiscordPreferences';
import { CandidateCard } from '../components/tradeDesk/CandidateCard';
import { AnswerCompare, SharedNotesBox } from '../components/tradeDesk/AnswerCompare';
import { defaultCandidateId, sharedNotes } from '../components/tradeDesk/answerFormat';
import { QuestionList } from '../components/tradeDesk/QuestionList';
import { ARCHIVE_REASONS, DEFAULT_FORM, parseNumber, parseInteger, formatDate, heldSummary, liveIsBlocked, errorMessage, statusVariant, statusLabel, textValue } from '../components/tradeDesk/deskFormat';
import type { AdviceFormState } from '../components/tradeDesk/deskFormat';

type TradeDeskView = 'opportunities' | 'holdings' | 'journal';
const TRADE_DESK_VIEWS: TradeDeskView[] = ['opportunities', 'holdings', 'journal'];
const DEFAULT_PREFERENCES: TradePreferences = {
  discordEnabled: false,
};

const TradeDeskPage: React.FC = () => {
  const { t } = useUiLanguage();
  const [searchParams, setSearchParams] = useSearchParams();
  const deepLinkTicker = searchParams.get('ticker')?.trim().toUpperCase() || '';
  const linkedAdviceId = searchParams.get('adviceId');
  const sourceReportIdRaw = searchParams.get('sourceReportId');
  const sourceReportId = sourceReportIdRaw && /^\d+$/.test(sourceReportIdRaw) ? Number(sourceReportIdRaw) : undefined;
  const [view, setView] = useState<TradeDeskView>(() => {
    const requested = searchParams.get('view');
    if (requested === 'ask') return 'opportunities';
    return TRADE_DESK_VIEWS.includes(requested as TradeDeskView) ? requested as TradeDeskView : 'opportunities';
  });
  const [health, setHealth] = useState<TradeDeskHealth | null>(null);
  const [catalog, setCatalog] = useState<TradeDeskCatalogItem[]>([]);
  // What "Auto" compares for each market view (from the server, so it matches the calculation).
  const [autoStrategies, setAutoStrategies] = useState<Record<string, string[]>>({});
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
  const [journal, setJournal] = useState<TradeJournalEvent[]>([]);
  const [preferences, setPreferences] = useState<TradePreferences>(DEFAULT_PREFERENCES);
  const [form, setForm] = useState<AdviceFormState>(() => ({ ...DEFAULT_FORM, ticker: deepLinkTicker }));
  const [selectedStrategies, setSelectedStrategies] = useState<string[]>([]);
  const [followUpForm, setFollowUpForm] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [busyAdviceId, setBusyAdviceId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const lastEventId = useRef('');
  // Incremented by each list request and each locally created job so an
  // older list response cannot drop a newer job or move the selection.
  const adviceListSequence = useRef(0);
  const archiveSequence = useRef(0);  // its own: opening the archive must not discard a list refresh in flight
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
    const results = await Promise.allSettled([tradeDeskApi.listJournal(), ...(includePreferences ? [tradeDeskApi.getPreferences()] : [])]);
    if (results[0].status === 'fulfilled') setJournal((results[0].value as { items?: TradeJournalEvent[] }).items || []);
    if (results[1]?.status === 'fulfilled') setPreferences(results[1].value as TradePreferences);
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
    if (results[1].status === 'fulfilled') { setCatalog(results[1].value.items || []); setAutoStrategies(results[1].value.defaults || {}); }
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
    if (linkedAdviceId) { setSelectedAdviceId(linkedAdviceId); setView('opportunities'); }
  }, [linkedAdviceId]);

  const activeAdviceIds = advice.filter((item) => item.status === 'queued' || item.status === 'running').map((item) => item.id).join(',');
  useEffect(() => {
    if (!activeAdviceIds) return undefined;
    const interval = window.setInterval(() => { void Promise.all(activeAdviceIds.split(',').map(async (id) => { try { const current = await tradeDeskApi.getAdvice(id); setAdvice((items) => items.map((item) => item.id === id ? current : item)); } catch { /* SSE and the next interval retry. */ } })); }, 2000);
    return () => window.clearInterval(interval);
  }, [activeAdviceIds]);

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
    const sequence = ++archiveSequence.current;
    try {
      const result = await tradeDeskApi.listAdvice('archive');
      if (sequence !== archiveSequence.current) return;
      setArchive(result.items || []);
      if (result.counts) setAdviceCounts(result.counts);
    } catch (archiveError) { setError(errorMessage(archiveError)); }
  };
  // The candidate open below the comparison, per answer (default: the desk's first pick).
  const [openCandidate, setOpenCandidate] = useState<Record<string, string>>({});

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
    archiveSequence.current += 1;
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

  const [repricingId, setRepricingId] = useState<string | null>(null);
  const reprice = async (job: TradeAdviceJob) => {
    setRepricingId(job.id); setError('');
    try {
      const updated = await tradeDeskApi.repriceAdvice(job.id);
      const swap = (items: TradeAdviceJob[]) => items.map((item) => (item.id === updated.id ? updated : item));
      setAdvice(swap);
      setArchive((items) => (items ? swap(items) : items));
      setPinned((items) => (items[updated.id] ? { ...items, [updated.id]: updated } : items));
    } catch (repriceError) { setError(errorMessage(repriceError)); } finally { setRepricingId(null); }
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
  const renderAnswer = () => {
    if (!selectedAdvice) return <Card variant="bordered" padding="md"><EmptyState icon={<FileQuestion className="h-8 w-8" />} title={t('tradeDesk.yourQuestions')} description={t('tradeDesk.pickQuestion')} /></Card>;
    const job = selectedAdvice;
    const running = job.status === 'queued' || job.status === 'running';
    return <Card variant="bordered" padding="none" className="p-3 sm:p-5"><div id="advice-detail" className="scroll-mt-4">
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
      {job.explanation ? <AdviceVerdict job={job} /> : null}
      {job.panel?.opinions?.length ? <ModelPanel panel={job.panel} /> : null}
      {job.status === 'stale' && (!job.repricedAt || job.invalidated?.length) ? <InlineAlert className="mt-3" variant="warning" message={t(job.invalidated?.length ? 'tradeDesk.invalidatedAdvice' : 'tradeDesk.staleAdvice')} action={<div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" isLoading={repricingId === job.id} onClick={() => void reprice(job)}>Refresh prices</Button><Button size="sm" variant="outline" isLoading={isSubmitting} onClick={() => void followUp(job, job.request.message || t('tradeDesk.runAgain'))}>{t('tradeDesk.runAgain')}</Button></div>} /> : null}
      {job.request.dataMode === 'live' && job.candidates.length && ['completed', 'stale'].includes(job.status) && (job.status !== 'stale' || (job.repricedAt && !job.invalidated?.length)) ? (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-secondary-text" data-testid="reprice-bar">
          <span>{job.repricedAt ? `Prices refreshed ${formatDate(job.repricedAt)}; the explanation was written at the earlier prices.` : `Prices as of ${formatDate(job.updatedAt || job.createdAt)}.`}{job.repriceFailed?.length ? ` ${job.repriceFailed.length} candidate(s) could not be re-priced and keep their earlier numbers.` : ''}</span>
          <Button size="xsm" variant="ghost" isLoading={repricingId === job.id} onClick={() => void reprice(job)}><RefreshCw className="h-3.5 w-3.5" />Refresh prices</Button>
        </div>
      ) : null}
      {job.candidates.length ? (() => {
        const shared = sharedNotes(job.candidates);
        const openId = job.candidates.some((item) => item.id === openCandidate[job.id]) ? openCandidate[job.id] : defaultCandidateId(job);
        const open = job.candidates.find((item) => item.id === openId);
        return <>
          <AnswerCompare job={job} selectedId={openId} onSelect={(candidate) => setOpenCandidate((current) => ({ ...current, [job.id]: candidate.id }))} />
          <SharedNotesBox notes={shared} />
          {open ? <CandidateCard key={open.id} candidate={open} advice={job} shared={shared} /> : null}
        </>;
      })() : null}
      {job.status === 'completed' && job.candidates.length === 0 && !job.explanation ? <div className="mt-5"><EmptyState title={t('tradeDesk.noCandidates')} description={t('tradeDesk.description')} /></div> : null}
      {job.nxTunnel || job.references?.length ? (
        <details className="mt-4 rounded-xl border border-border/40 bg-card/20 px-3 py-2" data-testid="answer-context">
          <summary className="cursor-pointer select-none text-sm text-secondary-text">Background the desk used: {[job.nxTunnel ? 'your NX tunnel' : '', ...(job.references || []).map((item) => item.title)].filter(Boolean).join(' · ')}</summary>
          <NxUsed job={job} />
          <ReferencesUsed job={job} />
        </details>
      ) : null}
      {!running ? <div className="mt-5 border-t border-border/50 pt-4">
        <textarea aria-label={t('tradeDesk.followUp')} value={followUpForm} onChange={(event) => setFollowUpForm(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && followUpForm.trim() && !isSubmitting) void followUp(job); }} rows={2} placeholder={t('tradeDesk.followUpPlaceholder')} className="input-surface w-full rounded-xl border px-3 py-2 text-sm text-foreground" />
        <div className="mt-2 flex justify-end"><Button size="sm" variant="outline" disabled={!followUpForm.trim()} isLoading={isSubmitting} onClick={() => void followUp(job)}><Sparkles className="h-4 w-4" />{t('tradeDesk.followUp')}</Button></div>
      </div> : null}
    </div></Card>;
  };
  const renderOpportunities = () => <div className="space-y-5"><AdviceForm form={form} setForm={setForm} catalog={catalog} autoStrategies={autoStrategies} health={health} selectedStrategies={selectedStrategies} setSelectedStrategies={setSelectedStrategies} onSubmit={() => void submitAdvice()} isSubmitting={isSubmitting} sourceReportId={sourceReportId} heldNote={heldNote} reportVerdict={currentVerdict} open={askPanelOpen} onToggle={() => setAskOpen(!askPanelOpen)} tickerOptions={tickerOptions} /><div className="grid gap-5 lg:grid-cols-[340px_minmax(0,1fr)] lg:items-start">{renderQuestions()}{renderAnswer()}</div></div>;
  // Replay events must never read as live: resolve each event's data mode from
  // its payload or advice so the journal can label it.
  const journalMode = (event: TradeJournalEvent): string | undefined => {
    const payload = event.payload || {};
    if (typeof payload.dataMode === 'string') return payload.dataMode;
    const adviceId = event.adviceId || (typeof payload.adviceId === 'string' ? payload.adviceId : undefined);
    return adviceId ? advice.find((item) => item.id === adviceId)?.request.dataMode : undefined;
  };
  const renderJournal = () => <div className="space-y-5"><TrackRecordCard /><TradeJournalCard /><div className="max-w-2xl"><DiscordPreferences preferences={preferences} onChange={setPreferences} onSave={() => void savePreferences()} /></div>{journal.length ? <Card variant="bordered" padding="md"><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.journal')}</h2><div className="mt-3 divide-y divide-border/40">{journal.map((event) => <div key={String(event.id)} className="grid min-w-0 gap-2 py-3 text-sm md:grid-cols-[150px_minmax(0,1fr)_180px]"><div className="flex min-w-0 flex-wrap items-start gap-2 font-medium text-foreground [overflow-wrap:anywhere]">{event.eventType}{journalMode(event) ? <ModeBadge mode={journalMode(event) as TradeDeskDataMode} /> : null}</div><div className="min-w-0 text-secondary-text [overflow-wrap:anywhere]">{Object.entries(event.payload || {}).map(([key, value]) => <span key={key} className="mr-3 inline-block max-w-full"><span className="text-muted-text">{key}</span>: {textValue(value)}</span>)}</div><div className="text-xs text-muted-text">{formatDate(event.createdAt)}</div></div>)}</div></Card> : <EmptyState icon={<BookOpen className="h-8 w-8" />} title={t('tradeDesk.noJournal')} description={t('tradeDesk.description')} />}</div>;

  if (isLoading && !health) return <AppPage><Loading label={t('common.loading')} /></AppPage>;
  if (health && !health.enabled) return <AppPage><InlineAlert variant="warning" title={t('tradeDesk.unavailable')} message="Trade Desk is disabled by the server configuration." /></AppPage>;
  return <AppPage><PageHeader eyebrow={t('tradeDesk.eyebrow')} title={t('tradeDesk.title')} description={t('tradeDesk.description')} actions={<><Button size="sm" variant="ghost" onClick={() => void refreshData()}><RefreshCw className="h-4 w-4" />{t('tradeDesk.refresh')}</Button><Link to="/settings" className="inline-flex h-9 items-center gap-2 rounded-lg border border-border/60 px-3 text-sm text-secondary-text hover:text-foreground"><Settings2 className="h-4 w-4" />Settings</Link></>} /><div className="mt-4 flex flex-wrap gap-2 rounded-2xl border border-border/50 bg-card/50 p-2" role="tablist" aria-label={t('tradeDesk.title')}>{([['opportunities', t('tradeDesk.ask')], ['holdings', t('tradeDesk.holdingsTab')], ['journal', t('tradeDesk.journal')]] as const).map(([key, label]) => <button key={key} id={`trade-desk-tab-${key}`} type="button" role="tab" aria-selected={view === key} aria-controls="trade-desk-panel" tabIndex={view === key ? 0 : -1} onKeyDown={onTabKey} onClick={() => setActiveView(key)} className={`rounded-xl px-4 py-2 text-sm transition ${view === key ? 'bg-cyan/10 text-cyan' : 'text-secondary-text hover:text-foreground'}`}>{label}</button>)}</div>{error ? <InlineAlert className="mt-4" variant="danger" title={t('common.failure')} message={error} action={<Button size="sm" variant="ghost" onClick={() => setError('')}>{t('common.close')}</Button>} /> : null}{message ? <InlineAlert className="mt-4" variant="success" message={message} /> : null}<ConfirmDialog isOpen={pendingDelete !== null} isDanger title={pendingDelete && 'job' in pendingDelete ? `Delete the ${pendingDelete.job.request.ticker} request?` : 'Delete all archived requests?'} message={pendingDelete && 'job' in pendingDelete ? 'The request and its answer are removed permanently. The journal keeps its log entries.' : `${pendingDelete && 'archive' in pendingDelete ? pendingDelete.archive : 0} archived requests are removed permanently.`} confirmText="Delete" onConfirm={() => void confirmDelete()} onCancel={() => setPendingDelete(null)} /><div className="mt-5" id="trade-desk-panel" role="tabpanel" aria-labelledby={`trade-desk-tab-${view}`}>{view === 'opportunities' ? renderOpportunities() : view === 'holdings' ? <HoldingsPanel onAsk={askAbout} /> : renderJournal()}</div></AppPage>;
};

export default TradeDeskPage;
