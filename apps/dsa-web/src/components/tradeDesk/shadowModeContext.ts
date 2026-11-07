import { createContext, useContext } from 'react';
import type { AssistantState } from '../../types/tradeDesk';

/** What the Trade Desk page shares about shadow mode (shadowMode.tsx provides it): one stream for every tab. */
export type NotifyPermission = NotificationPermission | 'unsupported';

export interface ShadowMode {
  state: AssistantState | null;
  setState: (next: AssistantState) => void;
  permission: NotifyPermission;
  allow: () => Promise<void>;
  /** Called on the click that turns shadow mode on: the browser lets a page play sound only after a click. */
  prime: () => void;
  markSeen: (seq: number) => void;
}

export const ShadowModeContext = createContext<ShadowMode | null>(null);

export function useShadowMode(): ShadowMode | null {
  return useContext(ShadowModeContext);
}
