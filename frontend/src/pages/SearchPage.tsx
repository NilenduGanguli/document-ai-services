/**
 * Search — hybrid (dense + lexical + structural) retrieval scoped to a client.
 *
 * Search is an explicit action rather than a live-as-you-type query: each run
 * costs an embedding call through the retrieval gateway.
 */
import { useCallback, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ApiError, search as runSearch } from '../lib/api';
import { useApiKey, useClientId, useMask } from '../hooks/useSettings';
import type { SearchHit, SearchResponse } from '../lib/types';
import { Badge, SensitivityBadge } from '../components/Badge';
import { IconPin, IconSearch, IconSpinner } from '../components/Icons';
import { ProvenanceDrawer } from '../components/ProvenanceDrawer';
import { EmptyState, ErrorState, NeedsClient, NeedsKey } from '../components/States';

function snippet(hit: SearchHit): string {
  return (
    hit.content ||
    hit.value_text ||
    hit.title ||
    hit.context_prefix ||
    '(no text on this node)'
  );
}

export function SearchPage(): JSX.Element {
  const [clientId] = useClientId();
  const [apiKey] = useApiKey();
  const [mask, setMask] = useMask();
  const [params, setParams] = useSearchParams();

  const [query, setQuery] = useState(params.get('q') ?? '');
  const [topK, setTopK] = useState(20);
  const [scopePath, setScopePath] = useState('');
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [provNode, setProvNode] = useState<SearchHit | null>(null);

  const docId = params.get('doc_id');

  const submit = useCallback(async () => {
    const q = query.trim();
    if (!q || !clientId) return;
    setLoading(true);
    setError(null);
    const next = new URLSearchParams(params);
    next.set('q', q);
    setParams(next, { replace: true });
    try {
      const res = await runSearch(clientId, {
        query: q,
        top_k: topK,
        mask,
        scope_path: scopePath.trim() || null,
        doc_id: docId,
      });
      setResult(res);
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(0, String(err)));
      setResult(null);
    } finally {
      setLoading(false);
    }
  }, [query, clientId, topK, mask, scopePath, docId, params, setParams]);

  if (!clientId) return <main className="page"><NeedsClient /></main>;
  if (!apiKey) return <main className="page"><NeedsKey /></main>;

  const hits = result?.hits ?? [];

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Search</h1>
          <p className="page-sub">
            Hybrid retrieval across this client&rsquo;s knowledge tree — dense vectors, lexical
            match and structural scope, ranked together.
          </p>
        </div>
      </div>

      <section className="card" style={{ marginBottom: 16 }}>
        <div className="card-body">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
          >
            <div className="row" style={{ gap: 10, alignItems: 'flex-end' }}>
              <label className="field" style={{ flex: 1, minWidth: 220 }}>
                <span className="field-label">Query</span>
                <input
                  className="input"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="e.g. what is the applicant's CURP?"
                  autoFocus
                />
              </label>

              <label className="field" style={{ width: 92 }}>
                <span className="field-label">Top K</span>
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={100}
                  value={topK}
                  onChange={(e) => setTopK(Math.max(1, Math.min(100, Number(e.target.value) || 1)))}
                />
              </label>

              <label className="field" style={{ minWidth: 160 }}>
                <span className="field-label">Scope path</span>
                <input
                  className="input"
                  value={scopePath}
                  onChange={(e) => setScopePath(e.target.value)}
                  placeholder="optional prefix"
                  spellCheck={false}
                />
              </label>

              <label className="switch" style={{ paddingBottom: 8 }}>
                <input type="checkbox" checked={mask} onChange={(e) => setMask(e.target.checked)} />
                <span className="switch-track" />
                Mask PII
              </label>

              <button
                type="submit"
                className="btn btn-primary"
                disabled={loading || !query.trim()}
                style={{ marginBottom: 1 }}
              >
                {loading ? <IconSpinner size={14} /> : <IconSearch size={14} />}
                {loading ? 'Searching…' : 'Search'}
              </button>
            </div>

            {docId && (
              <div className="row" style={{ marginTop: 10 }}>
                <Badge tone="info">scoped to doc {docId.slice(0, 8)}…</Badge>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => {
                    const next = new URLSearchParams(params);
                    next.delete('doc_id');
                    setParams(next);
                  }}
                >
                  Clear
                </button>
              </div>
            )}
          </form>
        </div>
      </section>

      {error && (
        <section className="card">
          <ErrorState error={error} onRetry={() => void submit()} />
        </section>
      )}

      {!error && !result && !loading && (
        <section className="card">
          <EmptyState
            title="Ask a question"
            text="Run a query to see ranked, grounded hits. Every hit links back to the page and bounding box it came from."
            icon={<IconSearch size={20} />}
          />
        </section>
      )}

      {!error && result && hits.length === 0 && (
        <section className="card">
          <EmptyState
            title="No hits"
            text={`Nothing matched “${result.query}”. Try a broader query, or widen the scope path.`}
            icon={<IconSearch size={20} />}
          />
        </section>
      )}

      {hits.length > 0 && (
        <>
          <div className="row" style={{ marginBottom: 12, justifyContent: 'space-between' }}>
            <span className="cell-muted">
              {result?.count} hit{result?.count === 1 ? '' : 's'} for{' '}
              <strong>&ldquo;{result?.query}&rdquo;</strong>
            </span>
          </div>
          <div className="stack" style={{ gap: 10 }}>
            {hits.map((hit, i) => (
              <article className="hit" key={hit.id}>
                <div className="hit-head">
                  <span className="hit-rank">{hit._rank ?? i + 1}</span>
                  <span className={`node-icon ${hit.node_type}`}>
                    {String(hit.node_type).charAt(0).toUpperCase()}
                  </span>
                  <span className="hit-path truncate" title={hit.path ?? ''}>
                    {hit.path ?? '(no path)'}
                  </span>
                  <span className="tree-spacer" />
                  {typeof hit._score === 'number' && (
                    <span className="hit-score" title="Hybrid relevance score">
                      {hit._score.toFixed(3)}
                    </span>
                  )}
                  {hit.masked && <Badge tone="neutral">masked</Badge>}
                  {hit.sensitivity && hit.sensitivity !== 'LOW' && (
                    <SensitivityBadge value={hit.sensitivity} />
                  )}
                </div>

                <p className="hit-snippet">{snippet(hit)}</p>

                <div className="row" style={{ marginTop: 10, justifyContent: 'space-between' }}>
                  <div className="row" style={{ gap: 6 }}>
                    <Badge tone="neutral">{hit.node_type}</Badge>
                    {hit.attribute_key && <Badge tone="gold">{hit.attribute_key}</Badge>}
                  </div>
                  <button type="button" className="btn btn-sm" onClick={() => setProvNode(hit)}>
                    <IconPin size={13} /> Provenance
                  </button>
                </div>
              </article>
            ))}
          </div>
        </>
      )}

      <ProvenanceDrawer
        nodeId={provNode?.id ?? null}
        clientId={clientId}
        titleHint={provNode?.attribute_key ?? provNode?.title ?? null}
        onClose={() => setProvNode(null)}
      />
    </main>
  );
}
