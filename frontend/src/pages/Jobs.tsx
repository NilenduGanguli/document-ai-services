/** Jobs — recent ingest runs, with cursor pagination and a status filter. */
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { listJobs } from '../lib/api';
import { formatAgo, formatTs, shortId } from '../lib/format';
import { useAsync } from '../hooks/useAsync';
import { useApiKey, useClientId } from '../hooks/useSettings';
import type { JobStatus } from '../lib/types';
import { JobStatusBadge } from '../components/Badge';
import { IconJobs, IconRefresh } from '../components/Icons';
import { EmptyState, ErrorState, NeedsClient, NeedsKey, SkeletonTable } from '../components/States';

const PAGE_SIZE = 25;
const FILTERS: Array<{ value: '' | JobStatus; label: string }> = [
  { value: '', label: 'All statuses' },
  { value: 'queued', label: 'Queued' },
  { value: 'running', label: 'Running' },
  { value: 'succeeded', label: 'Succeeded' },
  { value: 'failed', label: 'Failed' },
];

export function Jobs(): JSX.Element {
  const [clientId] = useClientId();
  const [apiKey] = useApiKey();
  const navigate = useNavigate();

  const [status, setStatus] = useState<'' | JobStatus>('');
  const [cursor, setCursor] = useState<string | null>(null);
  const [stack, setStack] = useState<string[]>([]);

  const enabled = !!clientId && !!apiKey;
  const { data, error, loading, reload } = useAsync(
    (signal) =>
      listJobs({ clientId, limit: PAGE_SIZE, cursor, status: status || null, signal }),
    [clientId, cursor, status],
    enabled,
  );

  if (!clientId) return <main className="page"><NeedsClient /></main>;
  if (!apiKey) return <main className="page"><NeedsKey /></main>;

  const jobs = data?.jobs ?? [];

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Jobs</h1>
          <p className="page-sub">Ingest runs for this client. Select a job to replay its stages.</p>
        </div>
        <div className="toolbar">
          <label className="field">
            <span className="field-label">Status</span>
            <select
              className="input input-inline"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value as '' | JobStatus);
                setCursor(null);
                setStack([]);
              }}
            >
              {FILTERS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="btn btn-sm" onClick={reload} disabled={loading}>
            <IconRefresh size={13} /> Refresh
          </button>
        </div>
      </div>

      <section className="card">
        {loading && !data && <SkeletonTable rows={6} cols={5} />}
        {error && <ErrorState error={error} onRetry={reload} />}
        {data && jobs.length === 0 && (
          <EmptyState
            title="No jobs"
            text={
              status
                ? `No ${status} jobs for this client.`
                : 'No ingest jobs have been submitted for this client yet.'
            }
            icon={<IconJobs size={20} />}
          />
        )}

        {jobs.length > 0 && (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Job</th>
                    <th scope="col">Document</th>
                    <th scope="col">Status</th>
                    <th scope="col">Stage</th>
                    <th scope="col">Created</th>
                    <th scope="col">Finished</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((j) => (
                    <tr
                      key={j.id}
                      className="clickable"
                      tabIndex={0}
                      onClick={() => navigate(`/job?id=${encodeURIComponent(j.id)}`)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          navigate(`/job?id=${encodeURIComponent(j.id)}`);
                        }
                      }}
                    >
                      <td className="mono cell-strong">{shortId(j.id)}</td>
                      <td>
                        <div className="truncate" style={{ maxWidth: 240 }}>
                          {j.document_name ?? '—'}
                        </div>
                        {j.error && (
                          <div className="cell-muted truncate" style={{ color: 'var(--red)', maxWidth: 240 }}>
                            {j.error}
                          </div>
                        )}
                      </td>
                      <td>
                        <JobStatusBadge status={j.status} />
                      </td>
                      <td className="cell-muted">{j.stage ?? '—'}</td>
                      <td className="cell-muted" title={formatTs(j.created_at)}>
                        {formatAgo(j.created_at)}
                      </td>
                      <td className="cell-muted" title={formatTs(j.finished_at)}>
                        {j.finished_at ? formatAgo(j.finished_at) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="pagination">
              <span className="cell-muted">
                Showing {jobs.length}
                {data?.next_cursor ? ' — more available' : ''}
              </span>
              <div className="row">
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={stack.length === 0 || loading}
                  onClick={() => {
                    const prev = [...stack];
                    prev.pop();
                    setStack(prev);
                    setCursor(prev.length ? (prev[prev.length - 1] as string) : null);
                  }}
                >
                  Previous
                </button>
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={!data?.next_cursor || loading}
                  onClick={() => {
                    const next = data?.next_cursor;
                    if (!next) return;
                    setStack((s) => [...s, next]);
                    setCursor(next);
                  }}
                >
                  Next
                </button>
              </div>
            </div>
          </>
        )}
      </section>
    </main>
  );
}
