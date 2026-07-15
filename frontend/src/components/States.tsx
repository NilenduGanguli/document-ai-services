/** Loading skeletons, empty states, and error states shared by every page. */
import type { ReactNode } from 'react';
import { ApiError } from '../lib/api';
import { IconAlert, IconInbox, IconRefresh } from './Icons';

export function Skeleton({
  width = '100%',
  height = 11,
  radius,
}: {
  width?: string | number;
  height?: string | number;
  radius?: number;
}): JSX.Element {
  return (
    <span
      className="skeleton"
      style={{
        display: 'block',
        width,
        height,
        ...(radius === undefined ? {} : { borderRadius: radius }),
      }}
    />
  );
}

/** A few shimmering lines, for card bodies. */
export function SkeletonLines({ lines = 3 }: { lines?: number }): JSX.Element {
  return (
    <div aria-hidden>
      {Array.from({ length: lines }, (_, i) => (
        <div
          key={i}
          className="skeleton skel-line"
          style={{ width: `${100 - i * 12}%` }}
        />
      ))}
    </div>
  );
}

/** Shimmering table rows that match the real column count. */
export function SkeletonTable({ rows = 5, cols = 4 }: { rows?: number; cols?: number }): JSX.Element {
  return (
    <div className="card-body" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading…</span>
      {Array.from({ length: rows }, (_, r) => (
        <div
          key={r}
          style={{
            display: 'grid',
            gridTemplateColumns: `repeat(${cols}, 1fr)`,
            gap: 12,
            marginBottom: 12,
          }}
          aria-hidden
        >
          {Array.from({ length: cols }, (_, c) => (
            <span
              key={c}
              className="skeleton"
              style={{ height: 11, width: c === 0 ? '80%' : '55%' }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  text,
  icon,
  action,
}: {
  title: string;
  text?: string;
  icon?: ReactNode;
  action?: ReactNode;
}): JSX.Element {
  return (
    <div className="state">
      <span className="state-icon">{icon ?? <IconInbox size={20} />}</span>
      <span className="state-title">{title}</span>
      {text && <p className="state-text">{text}</p>}
      {action}
    </div>
  );
}

/**
 * Renders an ApiError with an actionable message. 401/403 get specific,
 * self-explanatory copy instead of a bare status code.
 */
export function ErrorState({
  error,
  onRetry,
}: {
  error: ApiError;
  onRetry?: () => void;
}): JSX.Element {
  const title = error.isAuth
    ? 'Unauthorized'
    : error.isForbidden
      ? 'No access to this client'
      : error.status === 0
        ? 'Cannot reach the API'
        : error.isNotFound
          ? 'Not found'
          : // A 2xx that still errored means the body was not the JSON the
            // contract promises — usually an unimplemented endpoint caught by
            // the SPA fallback. "Request failed (200)" would read as nonsense.
            error.status >= 200 && error.status < 300
            ? 'Unexpected response'
            : `Request failed (${error.status})`;

  return (
    <div className="state error">
      <span className="state-icon">
        <IconAlert size={20} />
      </span>
      <span className="state-title">{title}</span>
      <p className="state-text">{error.friendly}</p>
      {onRetry && !error.isAuth && !error.isForbidden && (
        <button type="button" className="btn btn-sm" onClick={onRetry}>
          <IconRefresh size={13} /> Retry
        </button>
      )}
    </div>
  );
}

/** Guard shown when a page needs a client id but none is set. */
export function NeedsClient(): JSX.Element {
  return (
    <EmptyState
      title="No client selected"
      text="Enter a client ID in the header bar to scope this view. Every query is bound to a tenant."
    />
  );
}

/** Guard shown when a page needs an API key but none is set. */
export function NeedsKey(): JSX.Element {
  return (
    <EmptyState
      title="No API key set"
      text="Paste your API key into the header bar. It is stored in this browser's localStorage and sent as X-API-KEY on every request."
    />
  );
}
