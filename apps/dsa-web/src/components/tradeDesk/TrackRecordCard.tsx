import { useEffect, useState } from 'react';
import { BookOpen } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Card } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { TrackRecord } from '../../types/tradeDesk';

const NX_STATES = ['above', 'inside', 'below'] as const;
const SCOREBOARD_MIN = 30;
const VERDICTS: Record<string, { label: string; color?: string }> = {
  ahead: { label: 'Ahead of SPY', color: 'hsl(var(--color-success))' },
  behind: { label: 'Behind SPY — consider turning it down', color: 'hsl(var(--color-danger))' },
  no_difference: { label: 'No clear difference from SPY' },
  too_early: { label: 'Too early' },
};

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
  const nxGroups = record?.byNx ? Object.entries(record.byNx).filter(([, group]) => group.closed || group.open) : [];
  const nxClosed = nxGroups.reduce((sum, [, group]) => sum + group.closed, 0);
  const verdictRows = record?.verdicts ? Object.entries(record.verdicts).filter(([, group]) => (group.byNx.all?.closed ?? 0) + (group.byNx.all?.open ?? 0) > 0) : [];
  const verdictClosed = verdictRows.reduce((sum, [, group]) => sum + (group.byNx.all?.closed ?? 0), 0);
  return (
    <Card variant="bordered" padding="md">
      <div className="flex items-center gap-2">
        <BookOpen className="h-5 w-5 text-cyan" />
        <h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.trackRecord')}</h2>
      </div>
      <p className="mt-1 text-xs text-secondary-text">{t('tradeDesk.trackRecordHint')}</p>
      {problem ? <p className="mt-2 text-xs text-danger">{problem}</p> : null}
      {record && !groups.length && !nxGroups.length && !verdictRows.length && !record.scoreboard?.length ? <p className="mt-3 text-sm text-secondary-text">{t('tradeDesk.trackRecordEmpty')}</p> : null}
      {record?.scoreboard?.length ? (
        <div className="mt-3" data-testid="scoreboard">
          <h3 className="text-sm font-semibold text-foreground">What's working</h3>
          <p className="mt-0.5 text-xs text-secondary-text">Each source's average result vs SPY in the direction of its call. A verdict needs {SCOREBOARD_MIN} closed records and a clear difference across months.</p>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted-text">
                <tr><th className="py-1 text-left font-normal">Source</th><th className="pl-2 text-right font-normal">Closed</th><th className="hidden pl-2 text-right font-normal sm:table-cell">Open</th>
                  <th className="pl-2 text-right font-normal">vs SPY</th><th className="pl-3 text-left font-normal">So far</th></tr>
              </thead>
              <tbody className="divide-y divide-border/40">
                {record.scoreboard.map((item) => (
                  <tr key={item.key}>
                    <td className="py-1.5 text-foreground">{item.label}<span className="block text-[11px] text-muted-text">{item.horizon}</span></td>
                    <td className="pl-2 text-right">{item.closed}</td>
                    <td className="hidden pl-2 text-right sm:table-cell">{item.open}</td>
                    <td className="whitespace-nowrap pl-2 text-right">{pct(item.avgVsSpyPct)}</td>
                    <td className="pl-3 text-left text-xs" style={{ color: VERDICTS[item.verdict]?.color }}>
                      {item.verdict === 'too_early' ? `Too early (${item.closed}/${SCOREBOARD_MIN})` : VERDICTS[item.verdict]?.label ?? item.verdict}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
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
      {nxGroups.length ? (
        <div className="mt-4" data-testid="track-record-nx">
          <p className="text-xs font-semibold uppercase tracking-wide text-secondary-text">By your NX slow tunnel at the signal</p>
          <div className="mt-2 grid gap-2 sm:grid-cols-3">
            {nxGroups.map(([key, group]) => (
              <div key={key} className="rounded-lg bg-elevated/40 px-3 py-2 text-xs">
                <p className="font-semibold text-foreground">{group.label}</p>
                <p className="mt-1 text-secondary-text">{group.closed} closed · {group.open} open</p>
                <p className="text-secondary-text">win {pct(group.winRate, false)} · avg <span className={(group.avgReturnPct ?? 0) >= 0 ? 'text-success' : 'text-danger'}>{pct(group.avgReturnPct)}</span></p>
              </div>
            ))}
          </div>
          {nxClosed < 20 ? <p className="mt-1 text-xs text-muted-text">Too few closed ideas to judge NX yet; this fills in over the coming weeks.</p> : null}
        </div>
      ) : null}
      {verdictRows.length ? (
        <div className="mt-4" data-testid="track-record-verdicts">
          <p className="text-xs font-semibold uppercase tracking-wide text-secondary-text">Report calls by your NX slow tunnel · 10 sessions later vs SPY</p>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full min-w-[480px] text-xs">
              <thead className="text-muted-text">
                <tr><th className="py-1 text-left font-normal">Call</th>{NX_STATES.map((state) => <th key={state} className="text-right font-normal">{state === 'above' ? 'Above NX' : state === 'inside' ? 'Inside NX' : 'Below NX'}</th>)}<th className="text-right font-normal">Open</th></tr>
              </thead>
              <tbody className="divide-y divide-border/40">
                {verdictRows.map(([key, group]) => (
                  <tr key={key}>
                    <td className="py-1.5 text-foreground">{group.label}</td>
                    {NX_STATES.map((state) => {
                      const cell = group.byNx[state];
                      return <td key={state} className="text-right">{cell?.closed ? <><span className={(cell.avg10dVsSpyPct ?? 0) >= 0 ? 'text-success' : 'text-danger'}>{pct(cell.avg10dVsSpyPct)}</span> <span className="text-muted-text">({cell.closed})</span></> : '–'}</td>;
                    })}
                    <td className="text-right text-secondary-text">{group.byNx.all?.open ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {verdictClosed < 60 ? <p className="mt-1 text-xs text-muted-text">Calls settle 10 sessions after the report; few have closed so far.</p> : null}
        </div>
      ) : null}
      {record?.recent?.length ? (
        <div className="mt-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-secondary-text">{t('tradeDesk.trackRecent')}</p>
          <ul className="mt-2 grid gap-1 text-xs text-secondary-text sm:grid-cols-2">
            {record.recent.slice(0, 20).map((item, index) => (
              <li key={`${item.ticker}-${item.signalDay}-${index}`} className="flex justify-between gap-2 rounded bg-elevated/40 px-2 py-1">
                <span><span className="font-mono text-foreground">{item.ticker}</span> {item.direction} · {item.verdict} · {item.signalDay}{item.nxAlignment ? ` · NX ${item.nxAlignment}` : ''}</span>
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
