import { Settings2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Button, Card } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { TradePreferences } from '../../types/tradeDesk';

/** Discord message categories (server: trade_desk/discord_routes.py). */
export const DISCORD_CATEGORIES = [
  { key: 'holdings', label: 'Your positions', detail: 'Holding alerts, your price alerts, daily portfolio summary' },
  { key: 'market', label: 'Market moves & news', detail: 'Big moves and material news; names you do not hold arrive batched every 15 min' },
  { key: 'ideas', label: 'Trade ideas', detail: 'Swing opportunities, options ideas, breakouts' },
  { key: 'digest', label: 'Digests', detail: 'Social scan, weekly track record, system status' },
] as const;

export function DiscordPreferences({ preferences, onChange, onSave }: {
  preferences: TradePreferences;
  onChange: (next: TradePreferences) => void;
  onSave: () => void;
}) {
  const { t } = useUiLanguage();
  const categories = preferences.discordCategories ?? {};
  return (
    <Card variant="bordered" padding="md">
      <div className="flex items-center gap-2"><Settings2 className="h-5 w-5 text-cyan" /><h2 className="text-lg font-semibold text-foreground">{t('tradeDesk.preferences')}</h2></div>
      <div className="mt-4 space-y-3 text-sm">
        <label className="flex items-center justify-between gap-3">
          <span>{t('tradeDesk.discord')}</span>
          <input type="checkbox" checked={preferences.discordEnabled} onChange={(event) => onChange({ ...preferences, discordEnabled: event.target.checked })} />
        </label>
        <fieldset className="space-y-2 rounded-xl border border-border/40 p-3" disabled={!preferences.discordEnabled}>
          <legend className="px-1 text-xs text-secondary-text">Send these to Discord</legend>
          {DISCORD_CATEGORIES.map((category) => (
            <label key={category.key} className="flex items-start justify-between gap-3">
              <span><span className="text-foreground">{category.label}</span><span className="block text-xs text-muted-text">{category.detail}</span></span>
              <input type="checkbox" aria-label={category.label} checked={categories[category.key] !== false}
                onChange={(event) => onChange({ ...preferences, discordCategories: { ...categories, [category.key]: event.target.checked } })} />
            </label>
          ))}
          <p className="text-xs text-muted-text">Each category can post to its own channel: set TRADE_DESK_DISCORD_WEBHOOKS in .env.</p>
        </fieldset>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Button size="sm" onClick={onSave}>{t('tradeDesk.savePreferences')}</Button>
          <Link className="text-xs text-cyan hover:underline" to="/settings">{t('tradeDesk.discordSettings')}</Link>
        </div>
      </div>
    </Card>
  );
}
