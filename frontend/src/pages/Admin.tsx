/**
 * Admin — the danger zone, plus a read-only view of runtime readiness.
 *
 * Purging a client is irreversible and requires typing the client id, matching
 * the convention operators already know from GitHub/Stripe-style confirmations.
 */
import { useState } from 'react';
import { ApiError, deleteDocument, getReadyz, purgeClient } from '../lib/api';
import { useAsync } from '../hooks/useAsync';
import { useApiKey, useClientId } from '../hooks/useSettings';
import { Badge } from '../components/Badge';
import { IconAlert, IconRefresh, IconTrash } from '../components/Icons';
import { ErrorState, NeedsClient, NeedsKey, SkeletonLines } from '../components/States';
import { useToast } from '../components/Toast';

function DeletedSummary({ deleted }: { deleted: Record<string, unknown> }): JSX.Element {
  const entries = Object.entries(deleted);
  if (entries.length === 0) return <span className="cell-muted">Nothing was deleted.</span>;
  return (
    <div className="comp-extra">
      {entries.map(([k, v]) => (
        <span className="kv" key={k}>
          {k.replace(/_/g, ' ')} <b>{String(v)}</b>
        </span>
      ))}
    </div>
  );
}

export function Admin(): JSX.Element {
  const [clientId] = useClientId();
  const [apiKey] = useApiKey();
  const toast = useToast();

  const [docId, setDocId] = useState('');
  const [docBusy, setDocBusy] = useState(false);
  const [docResult, setDocResult] = useState<Record<string, unknown> | null>(null);

  const [confirmText, setConfirmText] = useState('');
  const [purgeBusy, setPurgeBusy] = useState(false);
  const [purgeResult, setPurgeResult] = useState<Record<string, unknown> | null>(null);

  const ready = useAsync((signal) => getReadyz(signal), []);

  const doDeleteDoc = async (): Promise<void> => {
    if (!docId.trim()) return;
    setDocBusy(true);
    setDocResult(null);
    try {
      const res = await deleteDocument(clientId, docId.trim());
      setDocResult(res.deleted);
      toast.success('Document deleted.');
      setDocId('');
    } catch (err) {
      const e = err instanceof ApiError ? err : new ApiError(0, String(err));
      toast.error(e.friendly);
    } finally {
      setDocBusy(false);
    }
  };

  const doPurge = async (): Promise<void> => {
    if (confirmText !== clientId) return;
    setPurgeBusy(true);
    setPurgeResult(null);
    try {
      const res = await purgeClient(clientId);
      setPurgeResult(res.deleted);
      toast.success(`Purged ${clientId}.`);
      setConfirmText('');
    } catch (err) {
      const e = err instanceof ApiError ? err : new ApiError(0, String(err));
      toast.error(
        e.isForbidden ? 'Purge requires an API key with admin scope.' : e.friendly,
      );
    } finally {
      setPurgeBusy(false);
    }
  };

  if (!clientId) return <main className="page"><NeedsClient /></main>;
  if (!apiKey) return <main className="page"><NeedsKey /></main>;

  const purgeArmed = confirmText === clientId;

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Admin</h1>
          <p className="page-sub">
            Destructive operations and runtime configuration. Everything here is scoped to{' '}
            <code>{clientId}</code>.
          </p>
        </div>
      </div>

      <div className="stack">
        {/* --- Delete a document --- */}
        <section className="danger-card">
          <div className="danger-head">
            <IconTrash size={15} /> Delete a document
          </div>
          <div className="card-body stack">
            <p className="cell-muted" style={{ margin: 0 }}>
              Removes one document, its versions, its subtree and every fact derived from it. Prefer
              the delete action on the Documents page, which shows you what you&rsquo;re removing.
            </p>
            <div className="row" style={{ gap: 10, alignItems: 'flex-end' }}>
              <label className="field" style={{ flex: 1, minWidth: 240 }}>
                <span className="field-label">Document ID</span>
                <input
                  className="input"
                  value={docId}
                  onChange={(e) => setDocId(e.target.value)}
                  placeholder="uuid"
                  spellCheck={false}
                />
              </label>
              <button
                type="button"
                className="btn btn-danger"
                disabled={!docId.trim() || docBusy}
                onClick={() => void doDeleteDoc()}
              >
                <IconTrash size={13} /> {docBusy ? 'Deleting…' : 'Delete'}
              </button>
            </div>
            {docResult && (
              <div className="banner green">
                <div>
                  <div className="banner-title">Deleted</div>
                  <DeletedSummary deleted={docResult} />
                </div>
              </div>
            )}
          </div>
        </section>

        {/* --- Purge client --- */}
        <section className="danger-card">
          <div className="danger-head">
            <IconAlert size={15} /> Purge client
          </div>
          <div className="card-body stack">
            <div className="banner red">
              <IconAlert size={15} />
              <div>
                <div className="banner-title">This erases everything for {clientId}</div>
                <div className="banner-body">
                  Every document, version, knowledge node, merged fact, job and stored blob for this
                  client is destroyed. There is no undo and no soft-delete. Requires an API key with
                  admin scope.
                </div>
              </div>
            </div>

            <label className="field" style={{ maxWidth: 420 }}>
              <span className="field-label">
                Type <code>{clientId}</code> to confirm
              </span>
              <input
                className="input"
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                placeholder={clientId}
                spellCheck={false}
                autoComplete="off"
                aria-describedby="purge-help"
              />
            </label>
            <span id="purge-help" className="sr-only">
              The purge button stays disabled until the typed value matches the client id exactly.
            </span>

            <div>
              <button
                type="button"
                className="btn btn-danger"
                disabled={!purgeArmed || purgeBusy}
                onClick={() => void doPurge()}
              >
                <IconAlert size={13} />
                {purgeBusy ? 'Purging…' : `Purge ${clientId}`}
              </button>
            </div>

            {purgeResult && (
              <div className="banner green">
                <div>
                  <div className="banner-title">Client purged</div>
                  <DeletedSummary deleted={purgeResult} />
                </div>
              </div>
            )}
          </div>
        </section>

        {/* --- Readiness (read-only) --- */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">Runtime readiness</div>
              <div className="card-sub">Read-only — reported by /readyz</div>
            </div>
            <button type="button" className="btn btn-sm" onClick={ready.reload}>
              <IconRefresh size={13} /> Refresh
            </button>
          </div>
          <div className="card-body">
            {ready.loading && !ready.data && <SkeletonLines lines={4} />}
            {ready.error && <ErrorState error={ready.error} onRetry={ready.reload} />}
            {ready.data && (
              <>
                <div className="row" style={{ marginBottom: 14 }}>
                  <Badge tone={ready.data.ready ? 'green' : 'red'} dot large>
                    {ready.data.ready ? 'ready' : 'not ready'}
                  </Badge>
                  {ready.data.degraded.map((d) => (
                    <Badge key={d} tone="amber">
                      {d} degraded
                    </Badge>
                  ))}
                </div>
                <pre className="json" style={{ maxHeight: 420 }}>
                  {JSON.stringify(ready.data, null, 2)}
                </pre>
              </>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
