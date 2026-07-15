/**
 * Ingest — upload a document, then watch the pipeline run.
 *
 * Uses the job flow (202 + poll `GET /jobs/{id}`) rather than the legacy
 * `?stream=true` SSE mode, so a reload or a nav-away never loses the run.
 */
import { useCallback, useRef, useState, type DragEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ApiError, ingest } from '../lib/api';
import { formatBytes } from '../lib/format';
import { useJobPolling } from '../hooks/useJobPolling';
import { useApiKey, useClientId } from '../hooks/useSettings';
import { JobStatusBadge } from '../components/Badge';
import { StageStepper } from '../components/StageStepper';
import { useToast } from '../components/Toast';
import { NeedsClient, NeedsKey } from '../components/States';
import {
  IconAlert,
  IconCheck,
  IconRefresh,
  IconSpinner,
  IconTree,
  IconUpload,
  IconX,
} from '../components/Icons';

const ACCEPT = '.pdf,.docx,.png,.jpg,.jpeg';
const ACCEPT_MIME = [
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'image/png',
  'image/jpeg',
];

function isAccepted(file: File): boolean {
  if (ACCEPT_MIME.includes(file.type)) return true;
  return /\.(pdf|docx|png|jpe?g)$/i.test(file.name);
}

export function Ingest(): JSX.Element {
  const [clientId] = useClientId();
  const [apiKey] = useApiKey();
  const toast = useToast();
  const navigate = useNavigate();

  const [file, setFile] = useState<File | null>(null);
  const [externalId, setExternalId] = useState('');
  const [idemKey, setIdemKey] = useState('');
  const [dragging, setDragging] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ApiError | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const { job, error: pollError, polling } = useJobPolling(jobId, clientId);

  const pick = useCallback(
    (f: File | null | undefined) => {
      if (!f) return;
      if (!isAccepted(f)) {
        toast.error(`${f.name} is not a supported type. Use PDF, DOCX, PNG or JPEG.`);
        return;
      }
      setFile(f);
      setSubmitError(null);
    },
    [toast],
  );

  const onDrop = useCallback(
    (e: DragEvent<HTMLElement>) => {
      e.preventDefault();
      setDragging(false);
      pick(e.dataTransfer.files?.[0]);
    },
    [pick],
  );

  const submit = useCallback(async () => {
    if (!file || !clientId) return;
    setSubmitting(true);
    setSubmitError(null);
    setJobId(null);
    try {
      const res = await ingest({
        clientId,
        file,
        externalDocumentId: externalId.trim() || undefined,
        idempotencyKey: idemKey.trim() || undefined,
      });
      setJobId(res.job_id);
      toast.success(`Accepted — job ${res.job_id.slice(0, 8)} queued.`);
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : new ApiError(0, String(err));
      setSubmitError(apiErr);
      toast.error(apiErr.friendly);
    } finally {
      setSubmitting(false);
    }
  }, [file, clientId, externalId, idemKey, toast]);

  const reset = useCallback(() => {
    setFile(null);
    setJobId(null);
    setSubmitError(null);
    setExternalId('');
    setIdemKey('');
    if (inputRef.current) inputRef.current.value = '';
  }, []);

  if (!clientId) {
    return (
      <main className="page">
        <NeedsClient />
      </main>
    );
  }
  if (!apiKey) {
    return (
      <main className="page">
        <NeedsKey />
      </main>
    );
  }

  const failed = job?.status === 'failed';
  const succeeded = job?.status === 'succeeded';

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Ingest</h1>
          <p className="page-sub">
            Upload a KYC document and watch it move through OCR, the sensitivity gate, extraction,
            subtree assembly and the client-level merge.
          </p>
        </div>
        {(job || file) && (
          <button type="button" className="btn btn-sm" onClick={reset}>
            <IconX size={13} /> Start over
          </button>
        )}
      </div>

      <div className="grid grid-2" style={{ alignItems: 'start' }}>
        {/* --- Upload --- */}
        <section className="card">
          <div className="card-head">
            <div className="card-title">Upload</div>
            <span className="cell-muted">PDF · DOCX · PNG · JPEG</span>
          </div>
          <div className="card-body stack">
            {!file ? (
              <>
                <button
                  type="button"
                  className={`dropzone${dragging ? ' dragging' : ''}`}
                  onClick={() => inputRef.current?.click()}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setDragging(true);
                  }}
                  onDragLeave={() => setDragging(false)}
                  onDrop={onDrop}
                >
                  <span className="dropzone-icon">
                    <IconUpload size={26} />
                  </span>
                  <span className="dropzone-title">Drop a document here</span>
                  <span className="dropzone-hint">or click to browse — PDF, DOCX, PNG, JPEG</span>
                </button>
                <input
                  ref={inputRef}
                  type="file"
                  accept={ACCEPT}
                  className="sr-only"
                  onChange={(e) => pick(e.target.files?.[0])}
                  aria-label="Choose a document to upload"
                />
              </>
            ) : (
              <div className="file-pill">
                <IconCheck size={16} style={{ color: 'var(--green)', flex: 'none' }} />
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div className="file-name" title={file.name}>
                    {file.name}
                  </div>
                  <div className="cell-muted">
                    {formatBytes(file.size)} · {file.type || 'unknown type'}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn-ghost btn-icon btn-sm"
                  onClick={() => setFile(null)}
                  aria-label="Remove selected file"
                  disabled={submitting}
                >
                  <IconX size={14} />
                </button>
              </div>
            )}

            <label className="field">
              <span className="field-label">External document ID (optional)</span>
              <input
                className="input"
                value={externalId}
                onChange={(e) => setExternalId(e.target.value)}
                placeholder="your system's ID for this document"
                spellCheck={false}
              />
            </label>

            <label className="field">
              <span className="field-label">Idempotency key (optional)</span>
              <input
                className="input"
                value={idemKey}
                onChange={(e) => setIdemKey(e.target.value)}
                placeholder="re-sending the same key will not re-ingest"
                spellCheck={false}
              />
            </label>

            <button
              type="button"
              className="btn btn-primary"
              disabled={!file || submitting || polling}
              onClick={() => void submit()}
            >
              {submitting ? <IconSpinner size={14} /> : <IconUpload size={14} />}
              {submitting ? 'Uploading…' : polling ? 'Pipeline running…' : 'Ingest document'}
            </button>

            {submitError && (
              <div className="banner red">
                <IconAlert size={15} />
                <div>
                  <div className="banner-title">Upload rejected</div>
                  <div className="banner-body">{submitError.friendly}</div>
                </div>
              </div>
            )}
          </div>
        </section>

        {/* --- Pipeline --- */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">Pipeline</div>
              <div className="card-sub">
                {job ? job.document_name || 'document' : 'awaiting an upload'}
              </div>
            </div>
            <div className="row">
              {polling && (
                <span className="cell-muted row" style={{ gap: 5 }}>
                  <IconSpinner size={12} /> polling
                </span>
              )}
              {job && <JobStatusBadge status={job.status} />}
            </div>
          </div>
          <div className="card-body">
            {!jobId ? (
              <div className="state">
                <span className="state-icon">
                  <IconUpload size={20} />
                </span>
                <span className="state-title">No run yet</span>
                <p className="state-text">
                  Upload a document to watch each stage report its engine, page count, gate decision
                  and fact counts as it lands.
                </p>
              </div>
            ) : (
              <>
                {failed && (
                  <div className="banner red" style={{ marginBottom: 14 }}>
                    <IconAlert size={15} />
                    <div>
                      <div className="banner-title">Pipeline failed at {job?.stage ?? 'unknown stage'}</div>
                      <div className="banner-body">{job?.error || 'No error detail was reported.'}</div>
                    </div>
                  </div>
                )}

                {succeeded && (
                  <div className="banner green" style={{ marginBottom: 14 }}>
                    <IconCheck size={15} />
                    <div style={{ flex: 1 }}>
                      <div className="banner-title">Ingest complete</div>
                      <div className="banner-body">
                        The document is now part of this client&rsquo;s knowledge tree.
                      </div>
                      {job?.doc_id && (
                        <div style={{ marginTop: 9 }}>
                          <Link
                            className="btn btn-sm"
                            to={`/tree?doc_id=${encodeURIComponent(job.doc_id)}`}
                          >
                            <IconTree size={13} /> Open knowledge tree
                          </Link>
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {pollError && (
                  <div className="banner red" style={{ marginBottom: 14 }}>
                    <IconAlert size={15} />
                    <div>
                      <div className="banner-title">Lost track of the job</div>
                      <div className="banner-body">{pollError.friendly}</div>
                    </div>
                  </div>
                )}

                <StageStepper job={job} />

                {job && (
                  <div className="row" style={{ marginTop: 16, justifyContent: 'space-between' }}>
                    <span className="cell-muted mono">job {job.id}</span>
                    <Link className="btn btn-sm" to={`/job?id=${encodeURIComponent(job.id)}`}>
                      <IconRefresh size={13} /> Job detail
                    </Link>
                  </div>
                )}

                {!job && !pollError && (
                  <div className="row" style={{ gap: 7 }}>
                    <IconSpinner size={14} />
                    <span className="cell-muted">Waiting for the first stage…</span>
                  </div>
                )}
              </>
            )}
          </div>
        </section>
      </div>

      {job?.doc_id && succeeded && (
        <div style={{ marginTop: 16 }}>
          <button type="button" className="btn" onClick={() => navigate('/documents')}>
            View all documents
          </button>
        </div>
      )}
    </main>
  );
}
