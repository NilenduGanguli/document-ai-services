import { useCallback, useEffect, useRef, type ReactNode } from 'react';
import { IconX } from './Icons';

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * A right-hand modal drawer.
 *
 * Accessibility: `role="dialog" aria-modal`, focus moves in on open and
 * returns to the invoking element on close, Tab is trapped inside, and Escape
 * closes. Background scroll is locked while open.
 */
export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}): JSX.Element | null {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  // Callers pass inline arrows for onClose. Reading it through a ref keeps the
  // effects below dependent on `open` alone — otherwise every parent re-render
  // would re-run them, and the cleanup's focus-restore would rip focus back out
  // of the drawer and thrash body.overflow.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (!open) return;
      if (e.key === 'Escape') {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab') return;
      const panel = panelRef.current;
      if (!panel) return;
      const nodes = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      if (nodes.length === 0) {
        e.preventDefault();
        return;
      }
      const first = nodes[0] as HTMLElement;
      const last = nodes[nodes.length - 1] as HTMLElement;
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    },
    [open],
  );

  // Keydown is registered via a ref-stable wrapper so this effect keys off
  // `open` only.
  const keyRef = useRef(handleKeyDown);
  keyRef.current = handleKeyDown;

  useEffect(() => {
    if (!open) return;
    const listener = (e: KeyboardEvent) => keyRef.current(e);
    document.addEventListener('keydown', listener, true);
    return () => document.removeEventListener('keydown', listener, true);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    // Focus the first control inside the panel (fall back to the panel itself).
    // Done synchronously rather than inside requestAnimationFrame: the DOM is
    // already committed when effects run, and rAF never fires while the tab is
    // hidden — which would silently strand focus outside the dialog.
    const panel = panelRef.current;
    if (panel) {
      const target = panel.querySelector<HTMLElement>(FOCUSABLE);
      (target ?? panel).focus();
    }

    return () => {
      document.body.style.overflow = prevOverflow;
      restoreRef.current?.focus?.();
    };
  }, [open]);

  if (!open) return null;

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} aria-hidden />
      <div
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        ref={panelRef}
        tabIndex={-1}
      >
        <div className="drawer-head">
          <div style={{ minWidth: 0 }}>
            <h2 className="drawer-title">{title}</h2>
            {subtitle && <div className="drawer-sub">{subtitle}</div>}
          </div>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close panel">
            <IconX size={16} />
          </button>
        </div>
        <div className="drawer-body">{children}</div>
        {footer}
      </div>
    </>
  );
}
