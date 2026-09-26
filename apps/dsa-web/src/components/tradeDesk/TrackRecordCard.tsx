import { useEffect, useState } from 'react';
import { BookOpen } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Card } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { TrackRecord } from '../../types/tradeDesk';

function pct(value?: number | null, signed = true): string {
  if (value == null || !Number.isFinite(value)) return '–';
  return signed ? `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` : `${value.toFixed(0)}%`;
}

/** Forward results of trade ideas (approved vs rejected by the review) and breakout alerts. */
export function TrackRecordCard() {
  const { t } = useUiLanguage();
  const [record, setRecord] = useState<TrackRecord | null>(null);
  const [problem, setProblem] = useState('');

  useEffect(() => {
    let active = true;
    tradeDeskApi.getTrackRecord()
      .then((value) => { if (active) setRecord(value); })
      .catch((error: unknown) => { if (active) setProblem(error instanceof Error ? error.message : String(error)); });
    return () => { active = false; };
  }, []);

  const groups = record ? Object.entries(record.groups).filter(([, group]) => group.closed || group.open) : [];
  return (
    <Card variant="bordered" padding="md">
      <div className="flex items-center gap-2">
        <BookOpen className="h-5 w-5 text-cyan" />
        <h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.trackRecord')}</h2>
      </div>
      <p className="mt-1 text-xs text-secondary-text">{t('tradeDesk.trackRecordHint')}</p>
      {problem ? <p className="mt-2 text-xs text-danger">{problem}</p> : null}
      {record && !groups.length ? <p className="mt-3 text-sm text-secondary-text">{t('tradeDesk.trackRecordEmpty')}</p> : null}
      {groups.length ? (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[520px] text-sm">
            <thead className="text-xs text-muted-text">
              <tr>
                <th className="py-1 text-left font-normal">{t('tradeDesk.trackGroup')}</th>
                <th className="text-right font-normal">{t('tradeDesk.trackClosed')}</th>
                <th className="text-right font-normal">{t('tradeDesk.winRate')}</th>
                <th className="text-right font-normal">{t('tradeDesk.trackAvg')}</th>
                <th className="text-right font-normal">{t('tradeDesk.trackVsSpy')}</th>
                <th className="text-right font-normal">{t('tradeDesk.trackOpen')}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {groups.map(([key, group]) => (
                <tr key={key}>
                  <td className="py-1.5 text-foreground">{group.label}</td>
                  <td className="text-right">{group.closed}</td>
                  <td className="text-right">{pct(group.winRate, false)}</td>
                  <td className={`text-right ${(group.avgReturnPct ?? 0) >= 0 ? 'text-success' : 'text-danger'}`}>{pct(group.avgReturnPct)}</td>
                  <td className="text-right">{pct(group.avgVsSpyPct)}</td>
                  <td className="text-right">{group.open}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {record?.recent?.length ? (
        <div className="mt-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-secondary-text">{t('tradeDesk.trackRecent')}</p>
          <ul className="mt-2 grid gap-1 text-xs text-secondary-text sm:grid-cols-2">
            {record.recent.slice(0, 20).map((item, index) => (
              <li key={`${item.ticker}-${item.signalDay}-${index}`} className="flex justify-between gap-2 rounded bg-elevated/40 px-2 py-1">
                <span><span className="font-mono text-foreground">{item.ticker}</span> {item.direction} · {item.verdict} · {item.signalDay}</span>
                <span className={item.status === 'closed' ? ((item.returnPct ?? 0) >= 0 ? 'text-success' : 'text-danger') : ''}>
                  {item.status === 'closed' ? `${pct(item.returnPct)} (${item.reason})` : `${t('tradeDesk.trackOpenNow')} ${pct(item.returnPct)}`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}

export default TrackRecordCard;
