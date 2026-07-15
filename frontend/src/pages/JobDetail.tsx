/** Job detail — the same live stepper as Ingest, addressable by URL. */
import { Link, useSearchParams } from 'react-router-dom';
import { formatTs } from '../lib/format';
import { useJobPolling } from '../hooks/useJobPolling';
import { useApiKey, useClientId } from '../hooks/useSettings';
import { JobStatusBadge } from '../components/Badge';
import { StageStepper } from '../components/StageStepper';
import { IconAlert, IconCheck, IconSpinner, IconTree } from '../components/Icons';
import { ErrorState, NeedsClient, NeedsKey, SkeletonLines } from '../components/States';

export function JobDetail(): JSX.Element {
  const [params] = useSearchParams();
  const jobId = params.get('id') ?? '';
  const [clientId] = useClientId();
  const [apiKey] = useApiKey();
  const { job, error, polling, loading } = useJobPolling(jobId || null, clientId);

  if (!clientId) return <main className="page"><NeedsClient /></main>;
  if (!apiKey) return <main className="page"><NeedsKey /></main>;

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Job</h1>
          <p className="page-sub mono">{jobId}</p>
        </div>
        <div className="toolbar">
          {polling && (
            <span className="cell-muted row" style={{ gap: 5 }}>
              <IconSpinner size={12} /> live
            </span>
          )}
          {job && <JobStatusBadge status={job.status} />}
          <Link className="btn btn-sm" to="/jobs">
            All jobs
          </Link>
        </div>
      </div>

      {error && <ErrorState error={error} />}

      {!error && (
        <div className="grid grid-2" style={{ alignItems: 'start' }}>
          <section className="card">
            <div className="card-head">
              <div className="card-title">Stages</div>
            </div>
            <div className="card-body">
              {loading && !job ? <SkeletonLines lines={7} /> : <StageStepper job={job} />}
            </div>
          </section>

          <section className="card">
            <div className="card-head">
              <div className="card-title">Summary</div>
            </div>
            <div className="card-body">
              {loading && !job ? (
                <SkeletonLines lines={5} />
              ) : (
                job && (
                  <>
                    {job.status === 'failed' && (
                      <div className="banner red" style={{ marginBottom: 16 }}>
                        <IconAlert size={15} />
                        <div>
                          <div className="banner-title">Failed at {job.stage ?? 'unknown stage'}</div>
                          <div className="banner-body">{job.error || 'No error detail reported.'}</div>
                        </div>
                      </div>
                    )}
                    {job.status === 'succeeded' && job.doc_id && (
                      <div className="banner green" style={{ marginBottom: 16 }}>
                        <IconCheck size={15} />
                        <div style={{ flex: 1 }}>
                          <div className="banner-title">Ingest complete</div>
                          <div style={{ marginTop: 9 }}>
                            <Link
                              className="btn btn-sm"
                              to={`/tree?doc_id=${encodeURIComponent(job.doc_id)}`}
                            >
                              <IconTree size={13} /> Open knowledge tree
                            </Link>
                          </div>
                        </div>
                      </div>
                    )}

                    <div className="prov-grid">
                      <span className="prov-key">Document</span>
                      <span className="prov-val">{job.document_name ?? '—'}</span>
                      <span className="prov-key">Client</span>
                      <span className="prov-val">{job.client_id}</span>
                      <span className="prov-key">Doc ID</span>
                      <span className="prov-val">{job.doc_id ?? '—'}</span>
                      <span className="prov-key">Version</span>
                      <span className="prov-val">{job.version_id ?? '—'}</span>
                      <span className="prov-key">Created</span>
                      <span className="prov-val">{formatTs(job.created_at)}</span>
                      <span className="prov-key">Updated</span>
                      <span className="prov-val">{formatTs(job.updated_at)}</span>
                      <span className="prov-key">Finished</span>
                      <span className="prov-val">{formatTs(job.finished_at)}</span>
                    </div>
                  </>
                )
              )}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
