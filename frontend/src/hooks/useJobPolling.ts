import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, getJob } from '../lib/api';
import type { Job, JobStatus } from '../lib/types';

const POLL_MS = 700;
const TERMINAL: ReadonlySet<JobStatus> = new Set<JobStatus>(['succeeded', 'failed']);

export function isTerminal(status: JobStatus | undefined | null): boolean {
  return !!status && TERMINAL.has(status);
}

export interface JobPolling {
  job: Job | null;
  error: ApiError | null;
  /** True while polling is active (i.e. a non-terminal job is being watched). */
  polling: boolean;
  loading: boolean;
  refresh: () => void;
}

/**
 * Poll `GET /jobs/{id}` every 700ms until the job reaches a terminal status.
 *
 * Stops on `succeeded`/`failed`, on unmount, and on any error that cannot
 * resolve by retrying (401/403/404) — a transient 5xx keeps polling. Pass a
 * null `jobId` to disable.
 */
export function useJobPolling(jobId: string | null, clientId: string): JobPolling {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [nonce, setNonce] = useState(0);
  const timerRef = useRef<number | null>(null);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  // Reset when the watched job changes, so a stale job never flashes.
  useEffect(() => {
    setJob(null);
    setError(null);
  }, [jobId]);

  useEffect(() => {
    if (!jobId || !clientId) {
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    let active = true;
    setLoading(true);

    const clear = () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };

    const tick = async (): Promise<void> => {
      try {
        const next = await getJob(jobId, clientId, controller.signal);
        if (!active) return;
        setJob(next);
        setError(null);
        setLoading(false);
        if (!isTerminal(next.status)) {
          timerRef.current = window.setTimeout(() => void tick(), POLL_MS);
        }
      } catch (err) {
        if (!active) return;
        if (err instanceof DOMException && err.name === 'AbortError') return;
        const apiErr = err instanceof ApiError ? err : new ApiError(0, String(err));
        setError(apiErr);
        setLoading(false);
        // Auth/permission/missing errors will not fix themselves — stop polling.
        const fatal = [401, 403, 404].includes(apiErr.status);
        if (!fatal) {
          timerRef.current = window.setTimeout(() => void tick(), POLL_MS * 2);
        }
      }
    };

    void tick();

    return () => {
      active = false;
      clear();
      controller.abort();
    };
  }, [jobId, clientId, nonce]);

  return { job, error, polling: !!job && !isTerminal(job.status), loading, refresh };
}
