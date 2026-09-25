import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { BarChart3, Clock3, RefreshCw, Sunrise, Sunset } from 'lucide-react';
import { historyApi } from '../api/history';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import { ApiErrorAlert, AppPage, Button, Card, PageHeader } from '../components/common';
import { ReportMarkdownPanel } from '../components/report/ReportMarkdownPanel';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { HistoryItem } from '../types/analysis';
import { cn } from '../utils/cn';
import { getReportSection, formatReportTime, type ReportSection } from '../utils/reportSessions';
import { tradeDeskTicker } from '../api/tradeDesk';

const SECTIONS = [
  { key: 'morning', icon: Sunrise },
  { key: 'intraday', icon: Clock3 },
  { key: 'closing', icon: Sunset },
  { key: 'market', icon: BarChart3 },
] as const;
const PAGE_SIZE = 50;

export default function ReportsPage() {
  const { language, t } = useUiLanguage();
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState<ReportSection | 'all'>('all');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const requestVersion = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    const version = ++requestVersion.current;
    let active = true;
    void historyApi.getList({ page, limit: PAGE_SIZE }, { signal: controller.signal })
      .then((response) => {
        if (!active || requestVersion.current !== version) return;
        setItems((previous) => page === 1 ? response.items : [
          ...previous, ...response.items.filter((item) => !previous.some((old) => old.id === item.id)),
        ]);
        setTotal(response.total);
      })
      .catch((reason: unknown) => {
        if (active && !controller.signal.aborted) setError(getParsedApiError(reason));
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [page, revision]);

  const visible = items.filter((item) => filter === 'all' || getReportSection(item) === filter);
  const selected = selectedId === -1 ? undefined : visible.find((item) => item.id === selectedId) ?? visible[0];
  const phase = selected?.marketPhaseSummary;
  const refresh = () => {
    setLoading(true);
    setError(null);
    setPage(1);
    setRevision((value) => value + 1);
  };

  return (
    <AppPage className="space-y-5">
      <PageHeader
        eyebrow={t('reports.eyebrow')}
        title={t('reports.title')}
        description={t('reports.description')}
        actions={(
          <>
            <Link to="/" className="btn-secondary">{t('reports.analyze')}</Link>
            <Button variant="secondary" onClick={refresh} disabled={loading}>
              <RefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
              {t('reports.refresh')}
            </Button>
          </>
        )}
      />
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4" aria-label={t('reports.sections')}>
        {SECTIONS.map(({ key, icon: Icon }) => {
          const matches = items.filter((item) => getReportSection(item) === key);
          return (
            <button key={key} type="button" aria-pressed={filter === key} onClick={() => setFilter(key)}
              className={cn('terminal-card rounded-2xl p-4 text-left transition-colors hover:bg-hover', filter === key && 'ring-2 ring-cyan')}>
              <Icon className="mb-3 h-5 w-5 text-cyan" />
              <span className="block font-semibold text-foreground">{t(`reports.section.${key}`)}</span>
              <span className="mt-1 block text-xs text-secondary-text">{t('reports.count', { count: matches.length })}</span>
              <span className="mt-2 block text-xs text-muted-text">
                {matches[0] ? formatReportTime(matches[0].createdAt, language) : t('reports.noSession')}
              </span>
            </button>
          );
        })}
      </div>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <button type="button" aria-pressed={filter === 'all'} onClick={() => setFilter('all')}
          className={cn('rounded-lg px-3 py-2', filter === 'all' ? 'bg-cyan/10 text-cyan' : 'text-secondary-text hover:bg-hover')}>
          {t('reports.all')}
        </button>
        <button type="button" aria-pressed={filter === 'other'} onClick={() => setFilter('other')}
          className={cn('rounded-lg px-3 py-2', filter === 'other' ? 'bg-cyan/10 text-cyan' : 'text-secondary-text hover:bg-hover')}>
          {t('reports.section.other')}
        </button>
        <span className="text-xs text-muted-text">{t('reports.loaded', { count: items.length, total })}</span>
        <span className="text-xs text-muted-text">{t('reports.timezone')}</span>
      </div>
      {error && <ApiErrorAlert error={error} />}
      {loading && <p role="status" className="text-sm text-secondary-text">{t('common.loading')}</p>}
      {!loading && !error && visible.length === 0 && (
        <Card><p className="font-medium text-foreground">{t('reports.empty')}</p>
          <p className="mt-2 text-sm text-secondary-text">{t('reports.emptyHint')}</p></Card>
      )}
      {visible.length > 0 && (
        <div className="grid items-start gap-5 lg:grid-cols-[minmax(220px,300px)_minmax(0,1fr)]">
          <div className="space-y-2 lg:max-h-[75vh] lg:overflow-y-auto" aria-label={t('reports.list')}>
            {visible.map((item) => (
              <button key={item.id} type="button" aria-pressed={selected?.id === item.id} onClick={() => setSelectedId(item.id)}
                className={cn('terminal-card w-full rounded-xl p-4 text-left hover:bg-hover', selected?.id === item.id && 'ring-1 ring-cyan')}>
                <span className="block font-semibold text-foreground">{item.stockName || item.stockCode}</span>
                <span className="mt-1 block text-xs text-cyan">{item.stockCode} · {t(`reports.section.${getReportSection(item)}`)}</span>
                <span className="mt-2 block text-xs text-muted-text">{formatReportTime(item.createdAt, language)}</span>
                {item.analysisSummary && <span className="mt-2 line-clamp-3 block text-sm text-secondary-text">{item.analysisSummary}</span>}
              </button>
            ))}
          </div>
          {selected && (
            <Card className="min-w-0 overflow-hidden" padding="lg">
              <div className="mb-5 space-y-2 border-b border-border pb-4 text-xs text-secondary-text">
                <p className="font-medium text-foreground">{t('reports.snapshot')}</p>
                <p>{t('reports.created')}: {formatReportTime(selected.createdAt, language)}</p>
                <p>{t('reports.context')}: {formatReportTime(phase?.marketLocalTime, language)}</p>
                <p>{t('reports.dailyBar')}: {phase?.effectiveDailyBarDate || '—'}</p>
                {phase?.isPartialBar && <p className="text-warning">{t('reports.partial')}</p>}
                {phase?.warnings?.map((warning, index) => <p key={`${index}-${warning}`} className="text-warning">{warning}</p>)}
              </div>
              {tradeDeskTicker(selected.stockCode) ? (
                <Link to={"/trade-desk?ticker=" + encodeURIComponent(tradeDeskTicker(selected.stockCode) || "") + "&sourceReportId=" + selected.id} className="mt-3 inline-flex rounded-lg border border-purple/40 px-3 py-1.5 text-xs font-semibold text-purple hover:bg-purple/10">
                  {t('tradeDesk.getAdvice')}
                </Link>
              ) : null}
              <ReportMarkdownPanel key={`${selected.id}:${revision}`} recordId={selected.id}
                stockName={selected.stockName || selected.stockCode} stockCode={selected.stockCode}
                reportLanguage={language} onRequestClose={() => setSelectedId(-1)} />
            </Card>
          )}
        </div>
      )}
      {items.length < total && (
        <Button variant="secondary" disabled={loading || Boolean(error)} onClick={() => {
          setLoading(true); setError(null); setPage((value) => value + 1);
        }}>{t('reports.older')}</Button>
      )}
      <p className="text-xs text-muted-text">{t('reports.scope')}</p>
    </AppPage>
  );
}
