/**
 * Facts — the merged, client-level resolved view.
 *
 * A fact can be sourced from several documents; `source_fact_ids` links back to
 * the underlying nodes, each of which carries its own provenance.
 */
import { useMemo, useState } from 'react';
import { getFacts } from '../lib/api';
import { humanize } from '../lib/format';
import { useAsync } from '../hooks/useAsync';
import { useApiKey, useClientId, useMask } from '../hooks/useSettings';
import type { MergedFact } from '../lib/types';
import { Badge, ConfidenceBar, SensitivityBadge, VerificationBadge } from '../components/Badge';
import { Drawer } from '../components/Drawer';
import { IconFacts, IconRefresh } from '../components/Icons';
import { ProvenanceDrawer } from '../components/ProvenanceDrawer';
import { EmptyState, ErrorState, NeedsClient, NeedsKey, SkeletonTable } from '../components/States';

export function Facts(): JSX.Element {
  const [clientId] = useClientId();
  const [apiKey] = useApiKey();
  const [mask, setMask] = useMask();

  const [verifiedOnly, setVerifiedOnly] = useState(false);
  const [filter, setFilter] = useState('');
  const [sourcesFor, setSourcesFor] = useState<MergedFact | null>(null);
  const [provNode, setProvNode] = useState<string | null>(null);

  const enabled = !!clientId && !!apiKey;
  const { data, error, loading, reload } = useAsync(
    (signal) => getFacts({ clientId, verifiedOnly, mask, signal }),
    [clientId, verifiedOnly, mask],
    enabled,
  );

  const facts = useMemo(() => {
    const all = data?.facts ?? [];
    const q = filter.trim().toLowerCase();
    if (!q) return all;
    return all.filter(
      (f) =>
        f.attribute_key.toLowerCase().includes(q) ||
        (f.resolved_value ?? '').toLowerCase().includes(q),
    );
  }, [data, filter]);

  if (!clientId) return <main className="page"><NeedsClient /></main>;
  if (!apiKey) return <main className="page"><NeedsKey /></main>;

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Merged facts</h1>
          <p className="page-sub">
            One resolved value per attribute for <code>{clientId}</code>, merged across every
            document. Conflicts and low-confidence values are flagged for review.
          </p>
        </div>
        <div className="toolbar">
          <label className="switch">
            <input
              type="checkbox"
              checked={verifiedOnly}
              onChange={(e) => setVerifiedOnly(e.target.checked)}
            />
            <span className="switch-track" />
            Verified only
          </label>
          <label className="switch">
            <input type="checkbox" checked={mask} onChange={(e) => setMask(e.target.checked)} />
            <span className="switch-track" />
            Mask PII
          </label>
          <button type="button" className="btn btn-sm" onClick={reload} disabled={loading}>
            <IconRefresh size={13} /> Refresh
          </button>
        </div>
      </div>

      <section className="card">
        <div className="card-head">
          <div className="card-title">
            {data ? `${facts.length} of ${data.count} facts` : 'Facts'}
          </div>
          <label className="field">
            <span className="sr-only">Filter facts</span>
            <input
              className="input input-inline"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="filter by key or value…"
              style={{ minWidth: 200 }}
            />
          </label>
        </div>

        {loading && !data && <SkeletonTable rows={7} cols={5} />}
        {error && <ErrorState error={error} onRetry={reload} />}
        {data && facts.length === 0 && (
          <EmptyState
            title={filter ? 'No matching facts' : 'No facts yet'}
            text={
              filter
                ? 'Nothing matches that filter.'
                : verifiedOnly
                  ? 'No facts have cleared the verification bar. Turn off "Verified only" to see everything.'
                  : 'Ingest a document to populate this client’s merged facts.'
            }
            icon={<IconFacts size={20} />}
          />
        )}

        {facts.length > 0 && (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Attribute</th>
                  <th scope="col">Resolved value</th>
                  <th scope="col">Confidence</th>
                  <th scope="col">Verification</th>
                  <th scope="col">Flags</th>
                  <th scope="col">Sensitivity</th>
                  <th scope="col">Sources</th>
                </tr>
              </thead>
              <tbody>
                {facts.map((f) => {
                  const sources = f.source_fact_ids ?? [];
                  return (
                    <tr key={f.attribute_key}>
                      <td>
                        <div className="cell-strong mono">{f.attribute_key}</div>
                        <div className="cell-muted">{humanize(f.attribute_key)}</div>
                      </td>
                      <td>
                        <span className="mono">{f.resolved_value ?? '—'}</span>
                        {f.value_date && <div className="cell-muted">date: {f.value_date}</div>}
                        {f.value_num !== null && f.value_num !== undefined && (
                          <div className="cell-muted">num: {f.value_num}</div>
                        )}
                      </td>
                      <td>
                        <ConfidenceBar value={f.confidence} />
                      </td>
                      <td>
                        <VerificationBadge value={f.verification_status} />
                      </td>
                      <td>
                        <div className="row" style={{ gap: 5 }}>
                          {f.verified && <Badge tone="green">verified</Badge>}
                          {f.conflict && (
                            <Badge tone="red" title="Sources disagree on this value.">
                              conflict
                            </Badge>
                          )}
                          {f.needs_review && (
                            <Badge tone="amber" title="Flagged for human review.">
                              needs review
                            </Badge>
                          )}
                          {f.masked && <Badge tone="neutral">masked</Badge>}
                          {!f.verified && !f.conflict && !f.needs_review && !f.masked && (
                            <span className="cell-muted">—</span>
                          )}
                        </div>
                      </td>
                      <td>
                        <SensitivityBadge value={f.sensitivity} />
                      </td>
                      <td>
                        {sources.length > 0 ? (
                          <button
                            type="button"
                            className="btn btn-sm"
                            onClick={() => setSourcesFor(f)}
                          >
                            {sources.length} source{sources.length === 1 ? '' : 's'}
                          </button>
                        ) : (
                          <span className="cell-muted">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Source picker → provenance */}
      <Drawer
        open={!!sourcesFor && !provNode}
        onClose={() => setSourcesFor(null)}
        title={sourcesFor?.attribute_key ?? 'Sources'}
        subtitle="Select a source node to see its provenance"
      >
        <div className="section-label">Resolved value</div>
        <div className="prov-grid">
          <span className="prov-key">Value</span>
          <span className="prov-val">{sourcesFor?.resolved_value ?? '—'}</span>
          <span className="prov-key">Confidence</span>
          <span className="prov-val">
            <ConfidenceBar value={sourcesFor?.confidence} />
          </span>
        </div>

        <div className="section-label">
          Source facts ({sourcesFor?.source_fact_ids?.length ?? 0})
        </div>
        <div className="stack" style={{ gap: 8 }}>
          {(sourcesFor?.source_fact_ids ?? []).map((id) => (
            <button
              key={id}
              type="button"
              className="btn"
              style={{ justifyContent: 'space-between', width: '100%' }}
              onClick={() => setProvNode(id)}
            >
              <span className="mono truncate">{id}</span>
              <span className="cell-muted">provenance →</span>
            </button>
          ))}
        </div>
      </Drawer>

      <ProvenanceDrawer
        nodeId={provNode}
        clientId={clientId}
        titleHint={sourcesFor?.attribute_key ?? null}
        onClose={() => setProvNode(null)}
      />
    </main>
  );
}
