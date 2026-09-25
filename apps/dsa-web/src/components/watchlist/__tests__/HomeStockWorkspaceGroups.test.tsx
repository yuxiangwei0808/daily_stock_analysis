import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';
import { HomeStockWorkspace } from '../HomeStockWorkspace';
import type { HomeWatchlistGroup, HomeWatchlistQuote, HomeWatchlistRow } from '../HomeStockWorkspace';

const rows: HomeWatchlistRow[] = ['AAPL', 'SPY', 'JPM'].map((code) => ({ code, analyzedToday: false }));

function renderWithGroups(groups: HomeWatchlistGroup[], quotes: Record<string, HomeWatchlistQuote> = {}) {
  render(
    <UiLanguageProvider>
      <HomeStockWorkspace
        activeTab="watchlist"
        onTabChange={vi.fn()}
        watchlistRows={rows}
        watchlistGroups={groups}
        watchlistQuotes={quotes}
        watchlistLoading={false}
        watchlistActioning={false}
        watchlistMessage={null}
        onAddToWatchlist={vi.fn()}
        onRemoveFromWatchlist={vi.fn()}
        onRefreshWatchlist={vi.fn()}
        onAnalyzeWatchlist={vi.fn()}
        isBatchAnalyzing={false}
        batchStatus={null}
        todayItems={[]}
        isLoadingTodayItems={false}
        todayLoadError={false}
        watchlistAnalyzedTodayCount={0}
        historyItems={[]}
        isLoadingHistory={false}
        selectedStockCode={undefined}
        selectedAssetType={null}
        selectedRecordId={undefined}
        onHistoryItemClick={vi.fn()}
      />
    </UiLanguageProvider>,
  );
}

describe('HomeStockWorkspace broker groups', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'en');
  });

  it('filters the watchlist by moomoo group and keeps ungrouped stocks reachable', () => {
    renderWithGroups([{ name: 'maga-7', codes: ['AAPL', 'MSFT'] }, { name: 'etf', codes: ['US.SPY'] },
      { name: 'empty', codes: ['DIS'] }]);
    const tabs = screen.getAllByRole('tab').map((tab) => tab.textContent);
    expect(tabs).toEqual(['All 3', 'maga-7 1', 'etf 1', 'Ungrouped 1']);  // groups without watchlist stocks are hidden
    expect(screen.getAllByText('JPM').length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('tab', { name: /etf/ }));
    expect(screen.getAllByText('SPY').length).toBeGreaterThan(0);
    expect(screen.queryAllByText('AAPL')).toHaveLength(0);
    expect(window.localStorage.getItem('dsa.home.watchlistGroup')).toBe('etf');

    fireEvent.click(screen.getByRole('tab', { name: /Ungrouped/ }));
    expect(screen.getAllByText('JPM').length).toBeGreaterThan(0);
    expect(screen.queryAllByText('SPY')).toHaveLength(0);
  });

  it('shows the flat list without tabs when groups are unavailable', () => {
    renderWithGroups([]);
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
    expect(screen.getAllByText('AAPL').length).toBeGreaterThan(0);
  });

  it('shows live prices, change and the extended-hours line', () => {
    renderWithGroups([], {
      AAPL: { price: 336.29, changePct: -0.22, session: 'postmarket', updatedAt: '2026-09-24 16:05:12',
        extended: { price: 337.1, changePct: 0.24 } },
      SPY: { price: 764.18, changePct: 0.47, session: 'postmarket', updatedAt: '2026-09-24 16:05:10' },
    });
    const quotes = screen.getAllByTestId('watchlist-quote').map((node) => node.textContent);
    expect(quotes).toEqual(['336.29-0.22%After 337.10 +0.24%', '764.18+0.47%']);
    expect(screen.getByTestId('watchlist-quote-status')).toHaveTextContent('Live quotes·After hours16:05:12 ET');
  });
});
