import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { tradeDeskApi } from '../api/tradeDesk';
import { AppPage, Button, Card, InlineAlert, PageHeader } from '../components/common';
import type { SystemStatus, SystemStatusComponent } from '../types/tradeDesk';

const REFRESH_MS = 30000;
const STATE_STYLE: Record<SystemStatusComponent['state'], { dot: string; label: string }> = {
  ok: { dot: 'bg-[hsl(var(--color-success))]', label: 'Working' },
  warn: { dot: 'bg-[hsl(var(--color-warning))]', label: 'Needs attention' },
  error: { dot: 'bg-[hsl(var(--color-danger))]', label: 'Failing' },
  off: { dot: 'bg-muted-text/40', label: 'Off' },
};
const OVERALL: Record<string, string> = {
  ok: 'Everything is working.',
  warn: 'Working, with something to look at below.',
  error: 'Something is failing — see the red rows.',
};

/** Server, quotes, scheduled reports and background jobs at a glance (refreshes every 30 seconds). */
export default function StatusPage() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await tradeDeskApi.getStatus());
      setError('');
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : 'The status could not be loaded');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const components = [...(status?.components ?? [])];
  return (
    <AppPage>
      <PageHeader eyebrow="System" title="Status" description="Server, quotes, scheduled reports and background jobs."
        actions={<Button size="sm" variant="ghost" onClick={() => void load()} disabled={loading}><RefreshCw className="h-4 w-4" />Refresh</Button>} />
      {error ? <InlineAlert className="mt-4" variant="danger" message={error} /> : null}
      {status ? (
        <>
          <div className="mt-4 flex items-center gap-3 rounded-2xl border border-border/50 bg-card/50 px-4 py-3" data-testid="status-overall">
            <span className={`h-3 w-3 shrink-0 rounded-full ${STATE_STYLE[status.overall as SystemStatusComponent['state']]?.dot ?? STATE_STYLE.ok.dot}`} />
            {/* A failed refresh keeps the last result on screen, marked as such. */}
            <span className="text-sm font-semibold text-foreground">{error ? 'Last known: ' : ''}{OVERALL[status.overall] ?? status.overall}</span>
            <span className="ml-auto text-xs text-muted-text">checked {new Date(status.checkedAt).toLocaleTimeString()}</span>
          </div>
          <Card variant="bordered" padding="none" className="mt-4 overflow-hidden">
            <ul className="divide-y divide-border/40" data-testid="status-components">
              {components.map((item) => (
                <li key={item.key} className="flex items-start gap-3 px-4 py-3">
                  <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${STATE_STYLE[item.state]?.dot ?? STATE_STYLE.off.dot}`} aria-hidden="true" />
                  <span className="sr-only">{STATE_STYLE[item.state]?.label}:</span>
                  <div className="min-w-0">
                    <p className={`text-sm font-medium ${item.state === 'off' ? 'text-muted-text' : 'text-foreground'}`}>{item.label}</p>
                    <p className="text-xs text-secondary-text [overflow-wrap:anywhere]">{item.detail}</p>
                  </div>
                </li>
              ))}
            </ul>
          </Card>
          <p className="mt-3 text-xs text-muted-text">A component still failing five minutes later is posted to Discord (once a day), and a summary is posted a couple of minutes after each server start.</p>
        </>
      ) : null}
    </AppPage>
  );
}
