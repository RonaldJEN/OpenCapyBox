import { createContext, useContext, useState } from 'react';

export const ActivityDisclosureScope = createContext('');

/** Persist only a user's disclosure preference, never conversation content. */
export function useActivityDisclosure(id: string, initial: boolean) {
  const scope = useContext(ActivityDisclosureScope);
  const key = scope ? `bsbox:activity:${scope}:${id}` : '';
  const [open, setOpen] = useState(() => {
    if (!key) return initial;
    try {
      const value = sessionStorage.getItem(key);
      return value === null ? initial : value === 'open';
    } catch { return initial; }
  });
  const set = (value: boolean) => {
    setOpen(value);
    if (key) {
      try { sessionStorage.setItem(key, value ? 'open' : 'closed'); } catch { /* The view still works without storage. */ }
    }
  };
  return [open, set] as const;
}
