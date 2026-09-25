import type { HistoryItem, ReportDetails } from '../types/analysis';

export type ReportSection = 'morning' | 'intraday' | 'closing' | 'market' | 'other';

export function getReportSection(item: HistoryItem): ReportSection {
  if (item.reportType === 'market_review') return 'market';
  switch (item.marketPhaseSummary?.phase) {
    case 'premarket': return 'morning';
    case 'intraday':
    case 'lunch_break':
    case 'closing_auction': return 'intraday';
    case 'postmarket': return 'closing';
    default: return 'other';
  }
}

export function formatReportTime(value: string | null | undefined, language: string): string {
  // A legacy timestamp without an offset cannot safely be converted to New York.
  if (!value) return '—';
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) return value;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(language === 'en' ? 'en-US' : 'zh-CN', {
    timeZone: 'America/New_York', month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
  }).format(parsed);
}

export function getReportQuote(details?: ReportDetails): Record<string, unknown> | undefined {
  const context = details?.contextSnapshot;
  const enhanced = context?.enhancedContext as Record<string, unknown> | undefined;
  const candidates = [details?.rawResult?.marketSnapshot, enhanced?.realtime,
    context?.realtimeQuoteRaw, context?.realtimeQuote];
  for (const candidate of candidates) {
    if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
      const quote = candidate as Record<string, unknown>;
      if (typeof quote.quoteSession === 'string') return quote;
    }
  }
  return undefined;
}
