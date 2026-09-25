import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { historyApi } from '../../api/history';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import type { HistoryItem } from '../../types/analysis';
import { UI_LANGUAGE_STORAGE_KEY } from '../../utils/uiLanguage';
import { formatReportTime, getReportSection } from '../../utils/reportSessions';
import ReportsPage from '../ReportsPage';

vi.mock('../../api/history', () => ({ historyApi: { getList: vi.fn(), getMarkdown: vi.fn() } }));
const reports: HistoryItem[] = [
  { id: 1, queryId: '1', stockCode: 'AAPL', stockName: 'Apple', reportType: 'detailed',
    createdAt: '2026-09-21T17:00:00-04:00', marketPhaseSummary: { phase: 'premarket', warnings: [], effectiveDailyBarDate: '2026-09-18' } },
  { id: 2, queryId: '2', stockCode: 'MSFT', stockName: 'Microsoft', createdAt: '2026-09-21T12:00:00-04:00',
    marketPhaseSummary: { phase: 'intraday', warnings: ['Incomplete session'], isPartialBar: true } },
  { id: 3, queryId: '3', stockCode: 'NVDA', stockName: 'Nvidia', createdAt: '2026-09-21T16:30:00-04:00' },
];
function mount() {
  return render(<UiLanguageProvider><MemoryRouter><ReportsPage /></MemoryRouter></UiLanguageProvider>);
}
beforeEach(() => {
  vi.resetAllMocks();
  localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'en');
  vi.mocked(historyApi.getList).mockResolvedValue({ total: 3, page: 1, limit: 50, items: reports });
  vi.mocked(historyApi.getMarkdown).mockImplementation(async (id) => `# Saved research ${id}`);
});

describe('Saved reports workspace', () => {
  it('uses saved phase instead of the report creation clock, and preserves unlabelled reports', async () => {
    mount();
    expect(await screen.findByRole('heading', { name: 'Saved research 1' })).toBeInTheDocument();
    fireEvent.click(within(screen.getByLabelText('Report sessions')).getByRole('button', { name: /Morning brief/ }));
    expect(screen.getByRole('button', { name: /Apple/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Microsoft/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Other / unspecified session' }));
    expect(await screen.findByRole('heading', { name: 'Saved research 3' })).toBeInTheDocument();
    expect(screen.getByText('Saved analysis — prices are not refreshed here.')).toBeInTheDocument();
  });
  it('shows missing closing reports rather than reclassifying older or unknown data', async () => {
    mount();
    await screen.findByRole('heading', { name: 'Saved research 1' });
    fireEvent.click(within(screen.getByLabelText('Report sessions')).getByRole('button', { name: /After the close/ }));
    expect(screen.getByText('No saved reports in this view')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /Saved research/ })).not.toBeInTheDocument();
  });
  it('retains intraday data-quality warnings and opens the matching persisted report', async () => {
    mount();
    await screen.findByRole('heading', { name: 'Saved research 1' });
    fireEvent.click(within(screen.getByLabelText('Report sessions')).getByRole('button', { name: /Intraday updates/ }));
    expect(await screen.findByRole('heading', { name: 'Saved research 2' })).toBeInTheDocument();
    expect(screen.getByText('Incomplete session')).toBeInTheDocument();
    expect(screen.getByText('The trading session was still in progress; intraday data may be incomplete.')).toBeInTheDocument();
  });
  it('recovers from a failed request without silently presenting an empty result', async () => {
    vi.mocked(historyApi.getList).mockRejectedValueOnce(new Error('History unavailable'));
    mount();
    await screen.findAllByText(/History unavailable/);
    expect(screen.queryByText('No saved reports in this view')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByRole('heading', { name: 'Saved research 1' });
    await waitFor(() => expect(historyApi.getList).toHaveBeenCalledTimes(2));
  });
  it('retries a failed report body on Refresh even when the selected record is unchanged', async () => {
    vi.mocked(historyApi.getMarkdown).mockRejectedValueOnce(new Error('Report body unavailable'));
    mount();
    await screen.findByText('Report body unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByRole('heading', { name: 'Saved research 1' })).toBeInTheDocument();
    expect(historyApi.getMarkdown).toHaveBeenCalledTimes(2);
  });
  it('distinguishes market reviews and non-trading reports', () => {
    expect(getReportSection({ ...reports[0], reportType: 'market_review' })).toBe('market');
    expect(getReportSection({ ...reports[0], marketPhaseSummary: { phase: 'non_trading', warnings: [] } })).toBe('other');
  });
  it('converts explicit offsets to New York but does not guess legacy timezones', () => {
    expect(formatReportTime('2026-09-22T01:00:00Z', 'en')).toContain('Sep 21');
    expect(formatReportTime('2026-09-22T01:00:00Z', 'en')).toContain('EDT');
    expect(formatReportTime('2026-01-22T01:00:00Z', 'en')).toContain('EST');
    expect(formatReportTime('2026-09-22T01:00:00', 'en')).toBe('2026-09-22T01:00:00');
  });
});
