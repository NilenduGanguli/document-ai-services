/** Documents — everything ingested for the active client. */
import { useCallback, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError, deleteDocument, listDocuments } from '../lib/api';
import { formatTs, shortId } from '../lib/format';
import { useAsync } from '../hooks/useAsync';
import { useApiKey, useClientId } from '../hooks/useSettings';
import type { DocumentRow } from '../lib/types';
import { Badge, ConfidenceBar, GateBadge, SensitivityBadge } from '../components/Badge';
import { Drawer } from '../components/Drawer';
import { IconAlert, IconDocs, IconRefresh, IconTrash } from '../components/Icons';
import { EmptyState, ErrorState, NeedsClient, NeedsKey, SkeletonTable } from '../components/States';
import { useToast } from '../components/Toast';

const PAGE_SIZE = 50;

export function Documents(): JSX.Element {
  const [clientId] = useClientId();
  const [apiKey] = useApiKey();
  const navigate = useNavigate();
  const toast = useToast();

  const [cursor, setCursor] = useState<string | null>(null);
  const [stack, setStack] = useState<string[]>([]);
  const [pendingDelete, setPendingDelete] = useState<DocumentRow | null>(null);
  const [deleting, setDeleting] = useState(false);

  const enabled = !!clientId && !!apiKey;
  const { data, error, loading, reload } = useAsync(
    (signal) => listDocuments({ clientId, limit: PAGE_SIZE, cursor, signal }),
    [clientId, cursor],
    enabled,
  );

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await deleteDocument(clientId, pendingDelete.id);
      toast.success(`Deleted ${pendingDelete.document_name ?? pendingDelete.id}.`);
      setPendingDelete(null);
      reload();
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : new ApiError(0, String(err));
      toast.error(apiErr.friendly);
    } finally {
      setDeleting(false);
    }
  }, [pendingDelete, clientId, toast, reload]);

  if (!clientId) return <main className="page"><NeedsClient /></main>;
  if (!apiKey) return <main className="page"><NeedsKey /></main>;

  const docs = data?.documents ?? [];

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Documents</h1>
          <p className="page-sub">
            Every document ingested for <code>{clientId}</code>. Select a row to open its knowledge
            tree.
          </p>
        </div>
        <button type="button" className="btn btn-sm" onClick={reload} disabled={loading}>
          <IconRefresh size={13} /> Refresh
        </button>
      </div>

      <section className="card">
        <div className="card-head">
          <div className="card-title">
            {data ? `${data.count} document${data.count === 1 ? '' : 's'}` : 'Documents'}
          </div>
        </div>

        {loading && !data && <SkeletonTable rows={6} cols={6} />}
        {error && <ErrorState error={error} onRetry={reload} />}
        {data && docs.length === 0 && (
          <EmptyState
            title="No documents yet"
            text="Nothing has been ingested for this client. Upload one from the Ingest page."
            icon={<IconDocs size={20} />}
            action={
              <button type="button" className="btn btn-sm btn-primary" onClick={() => navigate('/ingest')}>
                Go to Ingest
              </button>
            }
          />
        )}

        {docs.length > 0 && (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Document</th>
                    <th scope="col">Type</th>
                    <th scope="col">Jurisdiction</th>
                    <th scope="col">Sensitivity</th>
                    <th scope="col">Gate</th>
                    <th scope="col">Confidence</th>
                    <th scope="col">Pages</th>
                    <th scope="col">OCR</th>
                    <th scope="col">Created</th>
                    <th scope="col">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {docs.map((d) => (
                    <tr
                      key={d.id}
                      className="clickable"
                      tabIndex={0}
                      onClick={() => navigate(`/tree?doc_id=${encodeURIComponent(d.id)}`)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          navigate(`/tree?doc_id=${encodeURIComponent(d.id)}`);
                        }
                      }}
                    >
                      <td>
                        <div className="cell-strong truncate" style={{ maxWidth: 260 }}>
                          {d.document_name ?? shortId(d.id)}
                        </div>
                        <div className="cell-muted mono">
                          {d.external_document_id ? `ext: ${d.external_document_id}` : shortId(d.id)}
                        </div>
                      </td>
                      <td>
                        {d.doc_type ? (
                          <Badge tone="info">{d.doc_type}</Badge>
                        ) : (
                          <span className="cell-muted">—</span>
                        )}
                        {d.doc_category && <div className="cell-muted">{d.doc_category}</div>}
                      </td>
                      <td className="cell-muted">{d.jurisdiction ?? '—'}</td>
                      <td>
                        <SensitivityBadge value={d.sensitivity_bucket} />
                      </td>
                      <td>
                        <GateBadge value={d.gate_decision} />
                      </td>
                      <td>
                        <ConfidenceBar value={d.confidence} />
                      </td>
                      <td className="num cell-muted">{d.page_count ?? '—'}</td>
                      <td className="cell-muted">{d.ocr_engine ?? '—'}</td>
                      <td className="cell-muted">{formatTs(d.created_at)}</td>
                      <td>
                        <button
                          type="button"
                          className="btn btn-ghost btn-icon btn-sm"
                          aria-label={`Delete ${d.document_name ?? d.id}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            setPendingDelete(d);
                          }}
                        >
                          <IconTrash size={14} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="pagination">
              <span className="cell-muted">
                Showing {docs.length}
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

      <Drawer
        open={!!pendingDelete}
        onClose={() => setPendingDelete(null)}
        title="Delete document?"
        subtitle="This cannot be undone."
      >
        <div className="banner red" style={{ marginBottom: 16 }}>
          <IconAlert size={15} />
          <div>
            <div className="banner-title">Permanent</div>
            <div className="banner-body">
              This removes the document, its versions, its knowledge subtree and every fact derived
              from it. Merged client facts sourced only from this document will disappear.
            </div>
          </div>
        </div>
        <div className="prov-grid">
          <span className="prov-key">Name</span>
          <span className="prov-val">{pendingDelete?.document_name ?? '—'}</span>
          <span className="prov-key">Doc ID</span>
          <span className="prov-val">{pendingDelete?.id}</span>
          <span className="prov-key">Type</span>
          <span className="prov-val">{pendingDelete?.doc_type ?? '—'}</span>
        </div>
        <div className="row" style={{ marginTop: 22, justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={() => setPendingDelete(null)} disabled={deleting}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger"
            onClick={() => void confirmDelete()}
            disabled={deleting}
          >
            <IconTrash size={13} /> {deleting ? 'Deleting…' : 'Delete document'}
          </button>
        </div>
      </Drawer>
    </main>
  );
}
