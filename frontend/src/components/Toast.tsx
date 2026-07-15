import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { IconAlert, IconCheck, IconInfo, IconX } from './Icons';

type ToastKind = 'success' | 'error' | 'info';

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastApi {
  push: (kind: ToastKind, message: string) => void;
  success: (message: string) => void;
  error: (message: string) => void;
  info: (message: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const TTL_MS = 5200;

/** Transient notifications, announced politely to screen readers. */
export function ToastProvider({ children }: { children: ReactNode }): JSX.Element {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const timers = useRef<number[]>([]);

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (kind: ToastKind, message: string) => {
      const id = ++seq.current;
      setItems((prev) => [...prev.slice(-3), { id, kind, message }]);
      const handle = window.setTimeout(() => dismiss(id), TTL_MS);
      timers.current.push(handle);
    },
    [dismiss],
  );

  // Clear any pending timers on unmount.
  useEffect(() => {
    const handles = timers.current;
    return () => handles.forEach((h) => window.clearTimeout(h));
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      push,
      success: (m: string) => push('success', m),
      error: (m: string) => push('error', m),
      info: (m: string) => push('info', m),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`}>
            {t.kind === 'success' && <IconCheck size={15} />}
            {t.kind === 'error' && <IconAlert size={15} />}
            {t.kind === 'info' && <IconInfo size={15} />}
            <span className="toast-body">{t.message}</span>
            <button
              type="button"
              className="btn btn-ghost btn-icon btn-sm"
              onClick={() => dismiss(t.id)}
              aria-label="Dismiss notification"
            >
              <IconX size={13} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>');
  return ctx;
}
