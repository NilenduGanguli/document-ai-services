import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from 'react';
import { API_KEY_PERSIST_STORAGE, API_KEY_STORAGE, setApiKeyGetter } from '../lib/api';
import { useLocalStorage, useStorage } from './useLocalStorage';

export type Theme = 'light' | 'dark';

interface SettingsValue {
  apiKey: string;
  setApiKey: (v: string) => void;
  /** "Remember key on this device" — true moves the key from sessionStorage to localStorage. */
  apiKeyPersist: boolean;
  setApiKeyPersist: (v: boolean) => void;
  clientId: string;
  setClientId: (v: string) => void;
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
  /** Global masking projection toggle, forwarded as `mask=` to the API. */
  mask: boolean;
  setMask: (v: boolean) => void;
}

const SettingsContext = createContext<SettingsValue | null>(null);

const KEY_API = API_KEY_STORAGE;
const KEY_API_PERSIST = API_KEY_PERSIST_STORAGE;
const KEY_CLIENT = 'di.clientId';
const KEY_THEME = 'di.theme';
const KEY_MASK = 'di.mask';

/**
 * App-wide settings (API key, client id, theme, masking) shared through
 * context so a change in the header bar is seen immediately by every page in
 * the same tab.
 *
 * The API key defaults to sessionStorage (gone when the tab closes) rather
 * than localStorage: a persistent raw credential is exfiltratable by any XSS
 * and survives shared-workstation sessions. Checking "remember key on this
 * device" (apiKeyPersist) opts into localStorage for operators who accept
 * that trade on their own machine. Everything else here (client id, theme,
 * mask) is non-sensitive and stays in localStorage.
 */
export function SettingsProvider({ children }: { children: ReactNode }): JSX.Element {
  const [persistRaw, setPersistRaw] = useLocalStorage(KEY_API_PERSIST, 'false');
  const apiKeyPersist = persistRaw === 'true';

  const [sessionKey, setSessionKey] = useStorage(KEY_API, '', sessionStorage);
  const [localKey, setLocalKey] = useStorage(KEY_API, '', localStorage);

  // One-time migration on mount: this app used to persist the key in localStorage
  // unconditionally. Drop any stale value left over from before sessionStorage became the
  // default, unless the operator has explicitly opted into persistence.
  const migrated = useRef(false);
  if (!migrated.current) {
    migrated.current = true;
    if (!apiKeyPersist) {
      try {
        localStorage.removeItem(KEY_API);
      } catch {
        // best-effort
      }
    }
  }

  const apiKey = apiKeyPersist ? localKey : sessionKey;
  const setApiKey = useCallback(
    (v: string) => (apiKeyPersist ? setLocalKey(v) : setSessionKey(v)),
    [apiKeyPersist, setLocalKey, setSessionKey],
  );
  const setApiKeyPersist = useCallback(
    (v: boolean) => {
      setPersistRaw(v ? 'true' : 'false');
      if (v) {
        // Moving to persistent storage: carry the current session value over.
        setLocalKey(sessionKey);
        try {
          sessionStorage.removeItem(KEY_API);
        } catch {
          // best-effort
        }
      } else {
        // Moving back to session-only: carry the current persisted value over, then drop it
        // from localStorage so it doesn't outlive the tab after all.
        setSessionKey(localKey);
        try {
          localStorage.removeItem(KEY_API);
        } catch {
          // best-effort
        }
      }
    },
    [setPersistRaw, setLocalKey, setSessionKey, sessionKey, localKey],
  );

  const [clientId, setClientId] = useLocalStorage(KEY_CLIENT, '');
  const [themeRaw, setThemeRaw] = useLocalStorage(
    KEY_THEME,
    typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light',
  );
  const [maskRaw, setMaskRaw] = useLocalStorage(KEY_MASK, 'true');

  const theme: Theme = themeRaw === 'dark' ? 'dark' : 'light';
  const mask = maskRaw !== 'false';

  // Keep the api client reading the latest key without re-registering per render.
  // Registered during render, NOT in an effect: child effects run before parent
  // effects, so an effect here would land after the first page's fetch had
  // already gone out. Assignment is idempotent, so this is safe to repeat.
  const apiKeyRef = useRef(apiKey);
  apiKeyRef.current = apiKey;
  const registered = useRef(false);
  if (!registered.current) {
    registered.current = true;
    setApiKeyGetter(() => apiKeyRef.current);
  }

  // Reflect the theme onto <html> for the CSS custom-property scopes.
  useEffect(() => {
    const root = document.documentElement;
    root.classList.remove('theme-light', 'theme-dark');
    root.classList.add(`theme-${theme}`);
    root.style.colorScheme = theme;
  }, [theme]);

  const setTheme = useCallback((t: Theme) => setThemeRaw(t), [setThemeRaw]);
  const toggleTheme = useCallback(
    () => setThemeRaw(theme === 'dark' ? 'light' : 'dark'),
    [theme, setThemeRaw],
  );
  const setMask = useCallback((v: boolean) => setMaskRaw(v ? 'true' : 'false'), [setMaskRaw]);

  const value = useMemo<SettingsValue>(
    () => ({
      apiKey,
      setApiKey,
      apiKeyPersist,
      setApiKeyPersist,
      clientId,
      setClientId,
      theme,
      setTheme,
      toggleTheme,
      mask,
      setMask,
    }),
    [
      apiKey,
      setApiKey,
      apiKeyPersist,
      setApiKeyPersist,
      clientId,
      setClientId,
      theme,
      setTheme,
      toggleTheme,
      mask,
      setMask,
    ],
  );

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

function useSettings(): SettingsValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error('useSettings must be used inside <SettingsProvider>');
  return ctx;
}

/** The operator's API key. sessionStorage by default; see `useApiKeyPersist` to opt into localStorage. */
export function useApiKey(): [string, (v: string) => void] {
  const { apiKey, setApiKey } = useSettings();
  return [apiKey, setApiKey];
}

/** "Remember key on this device" — persists the API key to localStorage instead of sessionStorage. */
export function useApiKeyPersist(): [boolean, (v: boolean) => void] {
  const { apiKeyPersist, setApiKeyPersist } = useSettings();
  return [apiKeyPersist, setApiKeyPersist];
}

/** The active client id (RLS tenant scope), persisted in localStorage. */
export function useClientId(): [string, (v: string) => void] {
  const { clientId, setClientId } = useSettings();
  return [clientId, setClientId];
}

export function useTheme(): { theme: Theme; setTheme: (t: Theme) => void; toggle: () => void } {
  const { theme, setTheme, toggleTheme } = useSettings();
  return { theme, setTheme, toggle: toggleTheme };
}

export function useMask(): [boolean, (v: boolean) => void] {
  const { mask, setMask } = useSettings();
  return [mask, setMask];
}
