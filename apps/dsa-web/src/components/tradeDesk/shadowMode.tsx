import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { BellRing, Radio, Square } from 'lucide-react';
import { tradeDeskApi } from '../../api/tradeDesk';
import { Button } from '../common';
import type { AssistantEvent, AssistantState } from '../../types/tradeDesk';
import { nyTime } from './chartOverlays';
import { errorMessage } from './deskFormat';
import { ShadowModeContext, useShadowMode } from './shadowModeContext';
import type { NotifyPermission } from './shadowModeContext';

function canNotify() {
  return typeof window !== 'undefined' && 'Notification' in window;
}

function beep(context: AudioContext | null) {
  if (!context) return;
  try {
    const tone = context.createOscillator();
    const gain = context.createGain();
    tone.frequency.value = 880;
    gain.gain.setValueAtTime(0.15, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.25);
    tone.connect(gain).connect(context.destination);
    tone.start();
    tone.stop(context.currentTime + 0.25);
  } catch { /* no sound: the notification and the feed still show it */ }
}

/**
 * Shadow mode is an add-on to normal mode: while it is on, the trading assistant watches up to three stocks and
 * alerts on this page with a sound and a browser notification. Normal mode is what runs anyway: the Discord alerts.
 * One stream for the whole Trade Desk, so the alerts sound on every tab while the page is open.
 */
export function ShadowModeProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AssistantState | null>(null);
  const [permission, setPermission] = useState<NotifyPermission>(canNotify() ? Notification.permission : 'unsupported');
  const audio = useRef<AudioContext | null>(null);
  const seen = useRef(0);

  useEffect(() => {
    let live = true;
    void (async () => {
      try { const next = await tradeDeskApi.getAssistant(); if (live) { seen.current = next.seq; setState(next); } } catch { if (live) setState(null); }  // the desk is optional
    })();
    return () => { live = false; };
  }, []);

  const announce = useCallback((event: AssistantEvent) => {
    if (event.seq <= seen.current) return;
    seen.current = event.seq;
    if (event.kind === 'session' || event.kind === 'note') return;  // notes show on the page without a sound
    beep(audio.current);
    if (canNotify() && Notification.permission === 'granted') {
      try { new Notification(event.title, { body: event.detail, tag: `dsa-assistant-${event.seq}` }); } catch { /* the page's feed still shows it */ }
    }
  }, []);

  const available = state !== null;
  useEffect(() => {
    if (!available || typeof EventSource === 'undefined') return undefined;  // only once the desk answered
    const source = new EventSource(tradeDeskApi.getAssistantStreamUrl(), { withCredentials: true });
    const read = <T,>(event: Event): T | null => {
      try { return tradeDeskApi.readAssistant<T>(JSON.parse((event as MessageEvent<string>).data)); } catch { return null; }
    };
    source.addEventListener('state', (event) => { const next = read<AssistantState>(event); if (next) setState(next); });
    source.addEventListener('alert', (event) => { const next = read<AssistantEvent>(event); if (next) announce(next); });
    return () => source.close();
  }, [announce, available]);

  const prime = useCallback(() => {
    if (!audio.current && typeof window !== 'undefined' && 'AudioContext' in window) {
      try { audio.current = new AudioContext(); } catch { audio.current = null; }
    }
  }, []);
  const allow = useCallback(async () => {
    if (!canNotify()) return;
    try { setPermission(await Notification.requestPermission()); } catch { setPermission(Notification.permission); }
  }, []);
  const markSeen = useCallback((seq: number) => { seen.current = Math.max(seen.current, seq); }, []);

  const value = useMemo(() => ({ state, setState, permission, allow, prime, markSeen }), [state, permission, allow, prime, markSeen]);
  return <ShadowModeContext.Provider value={value}>{children}</ShadowModeContext.Provider>;
}

/** The Trade Desk's mode line, on every tab: normal mode always, shadow mode on top while it runs. */
export function ShadowModeBar({ onOpenChart }: { onOpenChart: () => void }) {
  const shadow = useShadowMode();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  if (!shadow?.state) return null;
  const { state } = shadow;
  const turnOff = async () => {
    setBusy(true); setError('');
    try { shadow.setState(await tradeDeskApi.stopAssistant()); } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(false); }
  };
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-border/50 bg-card/50 px-3 py-2 text-xs"
      role="status" aria-label="Trade Desk mode" data-testid="shadow-mode-bar">
      {state.running ? <>
        <span className="inline-flex items-center gap-1 font-semibold text-cyan"><Radio className="h-3.5 w-3.5" />Shadow mode on</span>
        <span className="text-secondary-text">{state.tickers.map((item) => item.ticker).join(', ')}
          {state.startedAt ? ` · since ${nyTime(state.startedAt)} ET` : ''} · alerts sound on this page while it is open; Discord alerts carry on as usual</span>
        {shadow.permission === 'default' ? <Button size="xsm" variant="ghost" onClick={() => void shadow.allow()}><BellRing className="h-3.5 w-3.5" />Allow notifications</Button> : null}
        <Button size="xsm" variant="outline" isLoading={busy} onClick={() => void turnOff()}><Square className="h-3.5 w-3.5" />Turn off</Button>
      </> : <>
        <span className="font-semibold text-foreground">Normal mode</span>
        <span className="text-secondary-text">Your alerts go to Discord. Shadow mode adds live alerts on this page for up to {state.maxTickers} stocks while you watch.</span>
        <Button size="xsm" variant="ghost" onClick={onOpenChart}>Set up shadow mode</Button>
      </>}
      {error ? <span role="alert" className="text-danger">{error}</span> : null}
    </div>
  );
}
