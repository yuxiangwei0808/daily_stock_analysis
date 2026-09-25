import type React from 'react';
import { useEffect, useMemo, useState } from 'react';
import {
  ArrowDownWideNarrow,
  CalendarDays,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Star,
  Trash2,
} from 'lucide-react';
import { Badge, Button, InlineAlert, Input, ScrollArea, StatusDot } from '../common';
import { DashboardPanelHeader, DashboardStateBlock } from '../dashboard';
import { StockBar } from '../history';
import type { StockBarItem, TaskInfo } from '../../types/analysis';
import { getSentimentColor } from '../../types/analysis';
import { buildDecisionActionLabelMap, getDecisionActionLabel } from '../../utils/decisionAction';
import { formatDateTime } from '../../utils/format';
import { areAssetAwareCodesEquivalent, toAssetAwareCodeKey, type AssetAwareAssetType } from '../../utils/stockCode';
import { truncateStockName } from '../../utils/stockName';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiTextKey, UiTextParams } from '../../i18n/uiText';

export type HomeWorkspaceTab = 'watchlist' | 'today' | 'history';
export type WatchlistAnalyzeMode = 'all' | 'pending';

export interface HomeWatchlistRow {
  code: string;
  assetType?: AssetAwareAssetType;
  /**
   * Asset-aware identity key already resolved by HomePage from the stock index
   * registry (canonical for registered indices, e.g. `sh000016` for a raw
   * watchlist string `000016.SH`). Row-selection compares against this key
   * FIRST instead of re-parsing the raw alias, so a canonical selected report
   * always selects its own alias-form row and never a same-code stock row.
   */
  identityKey?: string;
  latestItem?: StockBarItem;
  analyzedToday: boolean;
  isTodayStatusLoading?: boolean;
  isTodayStatusUnknown?: boolean;
  activeTask?: TaskInfo;
}

interface BatchStatus {
  variant: 'success' | 'warning' | 'danger';
  message: string;
}

export interface HomeWatchlistQuote {
  price: number;
  prevClose?: number | null;
  changePct?: number | null;
  updatedAt?: string;
  session?: string;
  extended?: { price: number; changePct?: number | null } | null;
}

export interface HomeWatchlistGroup {
  name: string;
  codes: string[];
}

const GROUP_STORAGE_KEY = 'dsa.home.watchlistGroup';
const ALL_GROUP = '__all__';
const OTHER_GROUP = '__other__';
const bareCode = (code: string) => code.trim().toUpperCase().replace(/^US\./, '').replace(/\.US$/, '');

function readStoredGroup(): string {
  try {
    return window.localStorage.getItem(GROUP_STORAGE_KEY) || ALL_GROUP;
  } catch {
    return ALL_GROUP;
  }
}

interface HomeStockWorkspaceProps {
  activeTab: HomeWorkspaceTab;
  /** Broker watchlist groups (moomoo custom groups), in app order. */
  watchlistGroups?: HomeWatchlistGroup[];
  /** Live quotes keyed by bare ticker; rows without one show no price. */
  watchlistQuotes?: Record<string, HomeWatchlistQuote>;
  onTabChange: (tab: HomeWorkspaceTab) => void;
  watchlistRows: HomeWatchlistRow[];
  watchlistLoading: boolean;
  watchlistActioning: boolean;
  watchlistMessage: string | null;
  onAddToWatchlist: (code: string) => Promise<void>;
  onRemoveFromWatchlist: (code: string) => Promise<void>;
  onRefreshWatchlist: () => Promise<void>;
  onAnalyzeWatchlist: (mode: WatchlistAnalyzeMode) => Promise<void>;
  isBatchAnalyzing: boolean;
  batchStatus: BatchStatus | null;
  todayItems: StockBarItem[];
  isLoadingTodayItems: boolean;
  todayLoadError: boolean;
  watchlistAnalyzedTodayCount: number;
  historyItems: StockBarItem[];
  isLoadingHistory: boolean;
  selectedStockCode?: string;
  selectedAssetType?: AssetAwareAssetType | null;
  selectedRecordId?: number;
  onHistoryItemClick: (recordId: number) => void;
  onDeleteStock?: (stockCode: string) => Promise<void> | void;
  isDeleting?: boolean;
  className?: string;
}

function getTaskStatusLabel(task: TaskInfo | undefined, t: (key: UiTextKey, params?: UiTextParams) => string) {
  if (!task) return '';
  if (task.status === 'processing') return t('taskPanel.processing');
  if (task.status === 'pending') return t('taskPanel.pending');
  if (task.status === 'cancel_requested') return t('taskPanel.cancelRequested');
  return task.status;
}

function rowHasSelectedIdentity(
  row: HomeWatchlistRow,
  selectedStockCode: string | null | undefined,
  selectedAssetType: AssetAwareAssetType | null | undefined,
): boolean {
  // The registry-derived canonical identity (HomePage) wins: an alias-form raw
  // watchlist code (e.g. `000016.SH`) whose identityKey is `sh000016` must
  // match the canonical selected report without re-parsing the raw alias, and
  // must never fold with the bare `000016` stock row.
  if (row.identityKey) {
    return toAssetAwareCodeKey(selectedStockCode, selectedAssetType) === row.identityKey;
  }
  return areAssetAwareCodesEquivalent(selectedStockCode, selectedAssetType, row.code, row.assetType);
}

const ScoreBadge: React.FC<{ item?: StockBarItem }> = ({ item }) => {
  const { t } = useUiLanguage();
  const score = typeof item?.sentimentScore === 'number' ? item.sentimentScore : null;
  const color = score !== null ? getSentimentColor(score) : null;
  if (score === null || !color) {
    return <span className="text-[11px] text-muted-text">{t('common.noData')}</span>;
  }

  const actionLabels = buildDecisionActionLabelMap(t);
  const operationLabel = getDecisionActionLabel(
    item?.action,
    item?.actionLabel,
    item?.operationAdvice,
    t('history.sentiment'),
    actionLabels,
  );

  return (
    <Badge
      variant="default"
      size="sm"
      className="shrink-0 shadow-none text-[11px] font-semibold leading-none"
      style={{
        color,
        borderColor: `${color}30`,
        backgroundColor: `${color}10`,
      }}
    >
      {operationLabel} {score}
    </Badge>
  );
};

const formatQuotePrice = (price: number) => price.toFixed(price >= 1 ? 2 : 4);
const formatChange = (pct: number) => `${pct > 0 ? '+' : ''}${pct.toFixed(2)}%`;
const changeTone = (pct?: number | null) =>
  pct == null || Math.abs(pct) < 0.005 ? 'neutral' : pct > 0 ? 'up' : 'down';
const CHANGE_PILL: Record<string, string> = {
  up: 'bg-success/15 text-success',
  down: 'bg-danger/15 text-danger',
  neutral: 'bg-base/60 text-secondary-text',
};

const QuoteCell: React.FC<{ quote: HomeWatchlistQuote }> = ({ quote }) => {
  const { t } = useUiLanguage();
  const [previousPrice, setPreviousPrice] = useState(quote.price);
  const [flash, setFlash] = useState<'up' | 'down' | null>(null);
  if (quote.price !== previousPrice) {
    // Adjusting state while rendering is React's pattern for reacting to a prop change.
    setFlash(quote.price > previousPrice ? 'up' : 'down');
    setPreviousPrice(quote.price);
  }
  useEffect(() => {
    if (!flash) return undefined;
    const timer = window.setTimeout(() => setFlash(null), 900);
    return () => window.clearTimeout(timer);
  }, [flash, quote.price]);
  const tone = changeTone(quote.changePct);
  const extendedLabel = quote.session === 'premarket' ? t('watchlist.preMarket') : t('watchlist.afterHours');
  return (
    <div className="flex flex-col items-end gap-1 text-right" data-testid="watchlist-quote">
      <span
        className={`rounded px-1 font-mono text-sm font-semibold tabular-nums transition-colors duration-700 ${
          flash === 'up' ? 'bg-success/20' : flash === 'down' ? 'bg-danger/20' : 'bg-transparent'
        } ${tone === 'up' ? 'text-success' : tone === 'down' ? 'text-danger' : 'text-foreground'}`}
      >
        {formatQuotePrice(quote.price)}
      </span>
      {quote.changePct != null ? (
        <span className={`min-w-[4.25rem] rounded-md px-1.5 py-0.5 text-center font-mono text-[11px] font-medium tabular-nums ${CHANGE_PILL[tone]}`}>
          {formatChange(quote.changePct)}
        </span>
      ) : null}
      {quote.extended ? (
        <span className="whitespace-nowrap font-mono text-[10px] tabular-nums text-muted-text">
          {extendedLabel} {formatQuotePrice(quote.extended.price)}
          {quote.extended.changePct != null ? (
            <span className={changeTone(quote.extended.changePct) === 'up' ? 'text-success' : changeTone(quote.extended.changePct) === 'down' ? 'text-danger' : ''}>
              {' '}{formatChange(quote.extended.changePct)}
            </span>
          ) : null}
        </span>
      ) : null}
    </div>
  );
};

const WatchlistRowItem: React.FC<{
  row: HomeWatchlistRow;
  onRemove: (code: string) => Promise<void>;
  onOpenDetail: (row: HomeWatchlistRow) => void;
  disabled: boolean;
  selected: boolean;
  quote?: HomeWatchlistQuote;
}> = ({ row, onRemove, onOpenDetail, disabled, selected, quote }) => {
  const { t } = useUiLanguage();
  const taskLabel = getTaskStatusLabel(row.activeTask, t);
  const isLatestDetailLoading = Boolean(row.isTodayStatusLoading);
  const isLatestDetailUnavailable = !isLatestDetailLoading && Boolean(row.isTodayStatusUnknown);
  const item = isLatestDetailLoading || isLatestDetailUnavailable ? undefined : row.latestItem;
  const stockName = row.latestItem?.stockName || row.code;
  const canOpenDetail = typeof item?.id === 'number';

  const handleOpenDetail = () => {
    onOpenDetail(row);
  };

  return (
    <div
      className={`home-subpanel group grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-3 py-2 text-left transition-colors ${
        selected
          ? 'border-primary/35 bg-primary/10'
          : 'hover:border-subtle-hover hover:bg-base/65'
      }`}
      data-testid={`watchlist-row-${row.code}`}
    >
      <button
        type="button"
        aria-pressed={selected}
        aria-label={canOpenDetail
          ? t('watchlist.openLatestDetailAria', { code: row.code })
          : isLatestDetailLoading
            ? t('watchlist.latestDetailLoadingAria', { code: row.code })
            : isLatestDetailUnavailable
              ? t('watchlist.latestDetailUnavailableAria', { code: row.code })
            : t('watchlist.noLatestDetailAria', { code: row.code })}
        className="grid min-w-0 cursor-pointer grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-lg text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan/30"
        onClick={handleOpenDetail}
      >
        <div className="min-w-0 space-y-0.5">
          <div className="flex min-w-0 items-center gap-2">
            <span className="truncate text-sm font-semibold text-foreground">
              {truncateStockName(stockName)}
            </span>
            {row.isTodayStatusLoading ? (
              <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-muted-text" aria-label={t('watchlist.todayStatusLoading')} />
            ) : row.isTodayStatusUnknown ? (
              <CircleAlert className="h-3.5 w-3.5 shrink-0 text-warning" aria-label={t('watchlist.todayStatusUnavailable')} />
            ) : row.analyzedToday ? (
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" aria-label={t('watchlist.analyzedToday')} />
            ) : (
              <Clock3 className="h-3.5 w-3.5 shrink-0 text-muted-text" aria-label={t('watchlist.notAnalyzedToday')} />
            )}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <span className="font-mono text-[11px] text-secondary-text">{row.code}</span>
            {item?.lastAnalysisTime ? (
              <>
                <span className="h-1 w-1 rounded-full bg-subtle-hover" />
                <span className="text-[11px] text-muted-text">{formatDateTime(item.lastAnalysisTime)}</span>
              </>
            ) : null}
          </div>
          {canOpenDetail ? null : (
            <div className="flex min-w-0 items-center justify-between gap-2 text-[11px]">
              <span className={`truncate ${isLatestDetailLoading ? 'text-muted-text' : 'text-warning'}`}>
                {isLatestDetailLoading
                  ? t('watchlist.latestDetailLoadingCta')
                  : isLatestDetailUnavailable
                    ? t('watchlist.latestDetailUnavailableCta')
                    : t('watchlist.noLatestDetailCta')}
              </span>
            </div>
          )}
          {row.activeTask ? (
            <div className="flex min-w-0 items-center gap-2 text-[11px] text-muted-text">
              <StatusDot
                tone={row.activeTask.status === 'processing' ? 'info' : 'neutral'}
                pulse={row.activeTask.status === 'processing'}
                className="h-1.5 w-1.5"
              />
              <span className="truncate">{t('watchlist.taskRunning', { status: taskLabel })}</span>
            </div>
          ) : null}
        </div>
        {quote ? <QuoteCell quote={quote} /> : null}
      </button>
      <div className="flex shrink-0 items-center gap-1">
        <ScoreBadge item={item} />
        <Button
          type="button"
          variant="ghost"
          size="xsm"
          className="h-7 w-7 px-0 opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
          disabled={disabled}
          aria-label={t('watchlist.removeAria', { code: row.code })}
          onClick={() => void onRemove(row.code)}
        >
          <Trash2 className="h-3.5 w-3.5 text-danger" aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
};

const TodayItem: React.FC<{ item: StockBarItem; onClick: (recordId: number) => void }> = ({ item, onClick }) => {
  const stockName = item.stockName || item.stockCode;

  return (
    <button
      type="button"
      className="home-subpanel grid w-full min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-3 py-2.5 text-left"
      onClick={() => onClick(item.id)}
    >
      <div className="min-w-0">
        <span className="block truncate text-sm font-semibold text-foreground">
          {truncateStockName(stockName)}
        </span>
        <span className="mt-1 block truncate font-mono text-[11px] text-secondary-text">
          {item.stockCode}
        </span>
      </div>
      <ScoreBadge item={item} />
    </button>
  );
};

export const HomeStockWorkspace: React.FC<HomeStockWorkspaceProps> = ({
  activeTab,
  onTabChange,
  watchlistRows,
  watchlistGroups = [],
  watchlistQuotes = {},
  watchlistLoading,
  watchlistActioning,
  watchlistMessage,
  onAddToWatchlist,
  onRemoveFromWatchlist,
  onRefreshWatchlist,
  onAnalyzeWatchlist,
  isBatchAnalyzing,
  batchStatus,
  todayItems,
  isLoadingTodayItems,
  todayLoadError,
  watchlistAnalyzedTodayCount,
  historyItems,
  isLoadingHistory,
  selectedStockCode,
  selectedAssetType,
  selectedRecordId,
  onHistoryItemClick,
  onDeleteStock,
  isDeleting = false,
  className = '',
}) => {
  const { t } = useUiLanguage();
  const [draftCode, setDraftCode] = useState('');
  const [selectedGroup, setSelectedGroup] = useState<string>(readStoredGroup);
  // Only groups that contain watchlist stocks are shown, like moomoo's tabs.
  const groupTabs = useMemo(() => {
    const rowCodes = new Set(watchlistRows.map((row) => bareCode(row.code)));
    const grouped = new Set<string>();
    const tabs = watchlistGroups
      .map((group) => {
        const codes = new Set(group.codes.map(bareCode).filter((code) => rowCodes.has(code)));
        codes.forEach((code) => grouped.add(code));
        return { key: group.name, label: group.name, codes };
      })
      .filter((tab) => tab.codes.size > 0);
    if (!tabs.length) return [];
    const other = new Set([...rowCodes].filter((code) => !grouped.has(code)));
    return [
      { key: ALL_GROUP, label: t('watchlist.groupAll'), codes: rowCodes },
      ...tabs,
      ...(other.size ? [{ key: OTHER_GROUP, label: t('watchlist.groupOther'), codes: other }] : []),
    ];
  }, [t, watchlistGroups, watchlistRows]);
  const quoteStatus = useMemo(() => {
    const quotes = Object.values(watchlistQuotes);
    if (!quotes.length) return null;
    const session = ['premarket', 'regular', 'postmarket'].includes(quotes[0].session ?? '') ? quotes[0].session! : 'closed';
    const updatedAt = quotes.map((quote) => quote.updatedAt ?? '').sort().pop() ?? '';
    return { session, updatedAt };
  }, [watchlistQuotes]);
  const activeGroup = groupTabs.find((tab) => tab.key === selectedGroup) ?? groupTabs[0];
  const visibleWatchlistRows = activeGroup && activeGroup.key !== ALL_GROUP
    ? watchlistRows.filter((row) => activeGroup.codes.has(bareCode(row.code)))
    : watchlistRows;
  const selectGroup = (key: string) => {
    setSelectedGroup(key);
    try {
      window.localStorage.setItem(GROUP_STORAGE_KEY, key);
    } catch {
      // Remembering the tab is a convenience only.
    }
  };
  // PR #2312: the notice carries the *triggering row's own* identity
  // (code + assetType). Reusing the candidate row's assetType for the notice
  // code would let an index row and a same-code stock row cross-match (e.g.
  // notice code ``000016.SH`` classified with the stock row's asset type folds
  // to stock ``000016`` and attaches to the wrong row).
  const [workspaceNotice, setWorkspaceNotice] = useState<{ code: string; assetType: AssetAwareAssetType } | null>(null);
  const pendingWatchlistCount = watchlistRows
    .filter((row) => !row.analyzedToday && !row.isTodayStatusLoading && !row.isTodayStatusUnknown)
    .length;
  const isTodayStatusUnavailable = watchlistRows.some((row) => row.isTodayStatusLoading || row.isTodayStatusUnknown);
  const topTodayItem = todayItems[0];
  const tabs: Array<{ key: HomeWorkspaceTab; label: string }> = [
    { key: 'history', label: t('watchlist.tabHistory') },
    { key: 'watchlist', label: t('watchlist.tabWatchlist') },
    { key: 'today', label: t('watchlist.tabToday') },
  ];

  const statusClassName = useMemo(() => {
    if (!batchStatus) return '';
    if (batchStatus.variant === 'danger') return 'border-danger/30 bg-danger/10 text-danger';
    if (batchStatus.variant === 'warning') return 'border-warning/30 bg-warning/10 text-warning';
    return 'border-success/30 bg-success/10 text-success';
  }, [batchStatus]);

  const visibleWorkspaceNotice = useMemo(() => {
    if (!workspaceNotice) return null;
    const row = watchlistRows.find(
      (item) => areAssetAwareCodesEquivalent(item.code, item.assetType, workspaceNotice.code, workspaceNotice.assetType),
    );
    if (!row) return null;
    if (row.isTodayStatusLoading) {
      return { message: t('watchlist.latestDetailLoading') };
    }
    if (row.isTodayStatusUnknown) {
      return { message: t('watchlist.latestDetailUnavailable') };
    }
    if (row.latestItem) return null;
    return { message: t('watchlist.noLatestDetail') };
  }, [t, watchlistRows, workspaceNotice]);

  const handleAddSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    const code = draftCode.trim();
    if (!code) return;
    setWorkspaceNotice(null);
    void onAddToWatchlist(code).then(() => setDraftCode(''));
  };

  const handleWatchlistRowOpen = (row: HomeWatchlistRow) => {
    if (row.isTodayStatusLoading || row.isTodayStatusUnknown) {
      setWorkspaceNotice({ code: row.code, assetType: row.assetType ?? 'stock' });
      return;
    }
    const recordId = row.latestItem?.id;
    if (typeof recordId === 'number') {
      setWorkspaceNotice(null);
      onHistoryItemClick(recordId);
      return;
    }
    setWorkspaceNotice({ code: row.code, assetType: row.assetType ?? 'stock' });
  };

  const renderTabs = (
    <div className="grid grid-cols-3 gap-1 rounded-xl border border-subtle bg-base/40 p-1">
      {tabs.map((tab) => {
        const selected = activeTab === tab.key;
        return (
          <button
            key={tab.key}
            type="button"
            aria-pressed={selected}
            className={`h-8 rounded-lg px-2 text-xs font-medium transition-colors ${
              selected ? 'bg-primary/15 text-primary shadow-inner' : 'text-secondary-text hover:bg-hover hover:text-foreground'
            }`}
            onClick={() => {
              setWorkspaceNotice(null);
              onTabChange(tab.key);
            }}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );

  if (activeTab === 'history') {
    return (
      <div
        data-testid="home-stock-workspace"
        className={`home-stock-scroll-shell flex min-h-0 flex-1 flex-col gap-2 ${className}`}
      >
        {renderTabs}
        <StockBar
          items={historyItems}
          isLoading={isLoadingHistory}
          selectedStockCode={selectedStockCode}
          selectedRecordId={selectedRecordId}
          onItemClick={onHistoryItemClick}
          onDeleteStock={onDeleteStock}
          isDeleting={isDeleting}
          className="flex-1"
        />
      </div>
    );
  }

  return (
    <aside
      data-testid="home-stock-workspace"
      className={`glass-card home-stock-scroll-shell flex min-h-0 flex-1 flex-col ${className}`}
    >
      <div className="space-y-2.5 border-b border-subtle px-3 py-3 sm:px-4">
        {renderTabs}

        {activeTab === 'watchlist' ? (
          <>
            <DashboardPanelHeader
              className="mb-0"
              title={t('watchlist.title')}
              titleClassName="text-sm font-medium"
              leading={<Star className="h-4 w-4 text-primary" aria-hidden="true" />}
              actions={(
                <div className="flex items-center gap-1.5">
                  <span className="text-[11px] text-muted-text">{t('common.itemsCount', { count: watchlistRows.length })}</span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="xsm"
                    className="h-7 w-7 px-0"
                    disabled={watchlistLoading}
                    onClick={() => {
                      setWorkspaceNotice(null);
                      void onRefreshWatchlist();
                    }}
                    aria-label={t('watchlist.refreshAria')}
                  >
                    <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                  </Button>
                </div>
              )}
            />
            <div className="flex flex-wrap gap-1.5">
              <Badge variant="default" className="gap-1 shadow-none text-[11px]">
                {t('watchlist.todayCoverage')} {watchlistAnalyzedTodayCount}/{watchlistRows.length}
              </Badge>
              <Badge variant="default" className="gap-1 shadow-none text-[11px]">
                {t('watchlist.pendingToday')} {pendingWatchlistCount}
              </Badge>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant="home-action-ai"
                className="h-8 flex-1 whitespace-nowrap px-2 text-xs sm:flex-none"
                disabled={watchlistLoading || watchlistRows.length === 0 || isBatchAnalyzing}
                isLoading={isBatchAnalyzing}
                loadingText={t('watchlist.submitting')}
                onClick={() => void onAnalyzeWatchlist('all')}
              >
                <Play className="h-4 w-4" aria-hidden="true" />
                {t('watchlist.analyzeAll')}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="home-action-report"
                className="h-8 flex-1 whitespace-nowrap px-2 text-xs sm:flex-none"
                disabled={watchlistLoading || pendingWatchlistCount === 0 || isTodayStatusUnavailable || isBatchAnalyzing}
                onClick={() => void onAnalyzeWatchlist('pending')}
              >
                <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                {t('watchlist.analyzePending')}
              </Button>
            </div>
            <form className="grid grid-cols-[minmax(0,1fr)_auto] gap-2" onSubmit={handleAddSubmit}>
              <Input
                value={draftCode}
                onChange={(event) => setDraftCode(event.target.value)}
                placeholder={t('watchlist.addPlaceholder')}
                className="h-8 rounded-lg px-3 text-xs"
                disabled={watchlistActioning}
                aria-label={t('watchlist.addPlaceholder')}
              />
              <Button
                type="submit"
                size="sm"
                variant="secondary"
                className="h-8 w-8 px-0"
                disabled={!draftCode.trim() || watchlistActioning}
                isLoading={watchlistActioning}
                aria-label={t('watchlist.add')}
              >
                <Plus className="h-4 w-4" aria-hidden="true" />
              </Button>
            </form>
            {batchStatus ? (
              <div className={`rounded-xl border px-3 py-2 text-xs ${statusClassName}`}>
                {batchStatus.message}
              </div>
            ) : null}
            {watchlistMessage ? (
              <div className="rounded-xl border border-subtle bg-base/35 px-3 py-2 text-xs text-secondary-text">
                {watchlistMessage}
              </div>
            ) : null}
            {visibleWorkspaceNotice ? (
              <InlineAlert
                variant="warning"
                message={visibleWorkspaceNotice.message}
                className="rounded-xl px-3 py-2 text-xs shadow-none"
              />
            ) : null}
          </>
        ) : (
          <>
            <DashboardPanelHeader
              className="mb-0"
              title={t('watchlist.todayTitle')}
              titleClassName="text-sm font-medium"
              leading={<CalendarDays className="h-4 w-4 text-cyan" aria-hidden="true" />}
              actions={<span className="text-[11px] text-muted-text">{t('common.itemsCount', { count: todayItems.length })}</span>}
            />
            <div className="flex flex-wrap gap-1.5">
              <Badge variant="default" className="gap-1 shadow-none text-[11px]">
                {t('watchlist.watchlistCoverage')} {watchlistAnalyzedTodayCount}/{watchlistRows.length}
              </Badge>
              <Badge variant="default" className="gap-1 shadow-none text-[11px]">
                {t('watchlist.topScore')} {topTodayItem?.sentimentScore ?? '-'}
              </Badge>
            </div>
          </>
        )}
      </div>

      <ScrollArea
        viewportClassName="px-3 py-3 sm:px-4 overscroll-y-contain touch-pan-y"
        className="min-h-0 flex-1"
        testId="home-stock-workspace-scroll"
      >
        {activeTab === 'watchlist' ? (
          watchlistLoading ? (
            <DashboardStateBlock loading compact title={t('watchlist.loading')} />
          ) : watchlistRows.length === 0 ? (
            <DashboardStateBlock
              compact
              title={t('watchlist.emptyTitle')}
              description={t('watchlist.emptyDescription')}
            />
          ) : (
            <div className="space-y-1.5">
              {quoteStatus ? (
                <div className="flex items-center gap-2 text-[11px] text-muted-text" data-testid="watchlist-quote-status">
                  <StatusDot
                    tone={quoteStatus.session === 'regular' ? 'success' : quoteStatus.session === 'closed' ? 'neutral' : 'info'}
                    pulse={quoteStatus.session !== 'closed'}
                    className="h-1.5 w-1.5"
                  />
                  <span className="font-medium text-secondary-text">{t('watchlist.liveQuotes')}</span>
                  <span>·</span>
                  <span>{t(`watchlist.session.${quoteStatus.session}` as UiTextKey)}</span>
                  {quoteStatus.updatedAt ? <span className="ml-auto font-mono tabular-nums">{quoteStatus.updatedAt.slice(11, 19)} ET</span> : null}
                </div>
              ) : (
                <div className="flex items-center gap-2 text-[11px] text-muted-text">
                  <ArrowDownWideNarrow className="h-3.5 w-3.5" aria-hidden="true" />
                  {t('watchlist.listHint')}
                </div>
              )}
              {groupTabs.length ? (
                <div className="flex gap-1.5 overflow-x-auto pb-1" role="tablist" aria-label={t('watchlist.groups')}>
                  {groupTabs.map((tab) => (
                    <button
                      key={tab.key}
                      type="button"
                      role="tab"
                      aria-selected={tab.key === activeGroup?.key}
                      onClick={() => selectGroup(tab.key)}
                      className={`shrink-0 rounded-full border px-2.5 py-1 text-[11px] transition-colors ${
                        tab.key === activeGroup?.key
                          ? 'border-primary/60 bg-primary/15 text-foreground'
                          : 'border-subtle text-secondary-text hover:text-foreground'
                      }`}
                    >
                      {tab.label} <span className="text-muted-text">{tab.codes.size}</span>
                    </button>
                  ))}
                </div>
              ) : null}
              {visibleWatchlistRows.map((row) => (
                <WatchlistRowItem
                  key={row.code}
                  row={row}
                  quote={watchlistQuotes[bareCode(row.code)]}
                  onRemove={async (code) => {
                    setWorkspaceNotice(null);
                    await onRemoveFromWatchlist(code);
                  }}
                  onOpenDetail={handleWatchlistRowOpen}
                  disabled={watchlistActioning}
                  selected={
                    (typeof selectedRecordId === 'number' && selectedRecordId === row.latestItem?.id)
                    || (
                      Boolean(selectedStockCode)
                      && (
                        rowHasSelectedIdentity(row, selectedStockCode, selectedAssetType)
                        || areAssetAwareCodesEquivalent(selectedStockCode ?? '', selectedAssetType, row.latestItem?.stockCode ?? '', row.latestItem?.assetType)
                      )
                    )
                  }
                />
              ))}
            </div>
          )
        ) : isLoadingTodayItems ? (
          <DashboardStateBlock loading compact title={t('watchlist.loading')} />
        ) : todayLoadError ? (
          <DashboardStateBlock
            compact
            title={t('watchlist.todayLoadErrorTitle')}
            description={t('watchlist.todayLoadErrorDescription')}
          />
        ) : todayItems.length === 0 ? (
          <DashboardStateBlock
            compact
            title={t('watchlist.todayEmptyTitle')}
            description={t('watchlist.todayEmptyDescription')}
          />
        ) : (
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-[11px] text-muted-text">
              <ArrowDownWideNarrow className="h-3.5 w-3.5" aria-hidden="true" />
              {t('watchlist.todaySortHint')}
            </div>
            {todayItems.map((item) => (
              <TodayItem key={`${item.stockCode}-${item.id}`} item={item} onClick={onHistoryItemClick} />
            ))}
          </div>
        )}
      </ScrollArea>
    </aside>
  );
};

export default HomeStockWorkspace;
