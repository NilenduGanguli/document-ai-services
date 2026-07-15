import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from 'react';
import { API_KEY_STORAGE, setApiKeyGetter } from '../lib/api';
import { useLocalStorage } from './useLocalStorage';

export type Theme = 'light' | 'dark';

interface SettingsValue {
  apiKey: string;
  setApiKey: (v: string) => void;
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
const KEY_CLIENT = 'di.clientId';
const KEY_THEME = 'di.theme';
const KEY_MASK = 'di.mask';

/**
 * App-wide settings (API key, client id, theme, masking) persisted to
 * localStorage and shared through context so a change in the header bar is
 * seen immediately by every page in the same tab.
 */
export function SettingsProvider({ children }: { children: ReactNode }): JSX.Element {
  const [apiKey, setApiKey] = useLocalStorage(KEY_API, '');
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
      clientId,
      setClientId,
      theme,
      setTheme,
      toggleTheme,
      mask,
      setMask,
    }),
    [apiKey, setApiKey, clientId, setClientId, theme, setTheme, toggleTheme, mask, setMask],
  );

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

function useSettings(): SettingsValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error('useSettings must be used inside <SettingsProvider>');
  return ctx;
}

/** The operator's API key, persisted in localStorage. */
export function useApiKey(): [string, (v: string) => void] {
  const { apiKey, setApiKey } = useSettings();
  return [apiKey, setApiKey];
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
