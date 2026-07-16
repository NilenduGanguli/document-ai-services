import { useCallback, useEffect, useState } from 'react';

/**
 * A `useState` that persists to a Web Storage backend and stays in sync
 * across tabs backed by the same storage.
 *
 * Reads/writes are wrapped in try/catch because both storages throw in
 * private-mode Safari and in sandboxed iframes.
 */
export function useStorage(
  key: string,
  initial: string,
  storage: Storage,
): [string, (value: string) => void] {
  const [value, setValue] = useState<string>(() => {
    try {
      return storage.getItem(key) ?? initial;
    } catch {
      return initial;
    }
  });

  const set = useCallback(
    (next: string) => {
      setValue(next);
      try {
        storage.setItem(key, next);
      } catch {
        // Persistence is best-effort; in-memory state still works.
      }
    },
    [key, storage],
  );

  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.storageArea === storage && e.key === key && e.newValue !== null) setValue(e.newValue);
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [key, storage]);

  return [value, set];
}

/** `useStorage` pinned to `localStorage` — the common case (theme, client id, etc). */
export function useLocalStorage(key: string, initial: string): [string, (value: string) => void] {
  return useStorage(key, initial, localStorage);
}
