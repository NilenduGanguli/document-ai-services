import { useCallback, useEffect, useState } from 'react';

/**
 * A `useState` that persists to localStorage and stays in sync across tabs.
 *
 * Reads are wrapped in try/catch because localStorage throws in private-mode
 * Safari and in sandboxed iframes.
 */
export function useLocalStorage(
  key: string,
  initial: string,
): [string, (value: string) => void] {
  const [value, setValue] = useState<string>(() => {
    try {
      return localStorage.getItem(key) ?? initial;
    } catch {
      return initial;
    }
  });

  const set = useCallback(
    (next: string) => {
      setValue(next);
      try {
        localStorage.setItem(key, next);
      } catch {
        // Persistence is best-effort; in-memory state still works.
      }
    },
    [key],
  );

  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === key && e.newValue !== null) setValue(e.newValue);
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [key]);

  return [value, set];
}
