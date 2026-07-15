/**
 * Dashboard — readiness + scale at a glance.
 *
 * Readiness comes from the unauthenticated `/readyz`, so this page renders
 * something useful even before an API key is set; the counts require a key and
 * a client id and degrade independently.
 */
import { Link } from 'react-router-dom';
import { getFacts, getReadyz, listDocuments, listJobs } from '../lib/api';
import { useAsync } from '../hooks/useAsync';
import { useApiKey, useClientId, useMask } from '../hooks/useSettings';
import type { ReadyComponent } from '../lib/types';
import { Badge } from '../components/Badge';
import { IconAlert, IconCheck, IconRefresh } from '../components/Icons';
import { EmptyState, ErrorState, Skeleton, SkeletonLines } from '../components/States';

/** Presentation metadata for the components /readyz reports. */
const COMPONENT_LABEL: Record<string, string> = {
  db: 'Database',
  migrations: 'Migrations',
  pgvector: 'pgvector',
  retrieval: 'Retrieval',
  ocr: 'OCR',
  blob: 'Blob storage',
};

const COMPONENT_BLURB: Record<string, string> = {
  db: 'Postgres connection pool + RLS tenant binding',
  migrations: 'Schema at head',
  pgvector: 'Vector index for dense retrieval',
  retrieval: 'Embedding / model gateway',
  ocr: 'Azure AI Vision Read (or local mock)',
  blob: 'Where raw uploaded bytes are retained',
};

function ComponentCard({ name, comp }: { name: string; comp: ReadyComponent }): JSX.Element {
  const extras = Object.entries(comp.extra ?? {});
  return (
    <div className={`comp ${comp.ok ? 'ok' : 'bad'}`}>
      <div className="comp-head">
        <span className="comp-name">
          {comp.ok ? (
            <IconCheck size={14} style={{ color: 'var(--green)' }} />
          ) : (
            <IconAlert size={14} style={{ color: 'var(--red)' }} />
          )}
          {COMPONENT_LABEL[name] ?? name}
        </span>
        <Badge tone={comp.ok ? 'green' : 'red'}>{comp.ok ? 'ok' : 'down'}</Badge>
      </div>
      <div className="comp-detail">
        {comp.detail || COMPONENT_BLURB[name] || 'No detail reported.'}
      </div>
      {extras.length > 0 && (
        <div className="comp-extra">
          {extras.map(([k, v]) => (
            <span className="kv" key={k}>
              {k.replace(/_/g, ' ')}{' '}
              <b title={String(v)}>{typeof v === 'boolean' ? (v ? 'yes' : 'no') : String(v)}</b>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  foot,
  tone,
  loading,
  to,
}: {
  label: string;
  value: string | number;
  foot?: string;
  tone?: 'gold' | 'green' | 'amber';
  loading?: boolean;
  to?: string;
}): JSX.Element {
  const inner = (
    <div className={`stat${tone ? ` ${tone}` : ''}`}>
      <div className="stat-label">{label}</div>
      {loading ? (
        <div style={{ marginTop: 8 }}>
          <Skeleton width="52%" height={22} />
        </div>
      ) : (
        <div className="stat-value">{value}</div>
      )}
      {foot && <div className="stat-foot">{foot}</div>}
    </div>
  );
  return to ? (
    <Link to={to} style={{ textDecoration: 'none', color: 'inherit' }}>
      {inner}
    </Link>
  ) : (
    inner
  );
}

export function Dashboard(): JSX.Element {
  const [clientId] = useClientId();
  const [apiKey] = useApiKey();
  const [mask] = useMask();

  const ready = useAsync((signal) => getReadyz(signal), []);
  const scoped = !!clientId && !!apiKey;

  const docs = useAsync(
    (signal) => listDocuments({ clientId, limit: 200, signal }),
    [clientId],
    scoped,
  );
  const facts = useAsync((signal) => getFacts({ clientId, mask, signal }), [clientId, mask], scoped);
  const jobs = useAsync(
    (signal) => listJobs({ clientId, limit: 100, signal }),
    [clientId],
    scoped,
  );

  const r = ready.data;
  const components = Object.entries(r?.components ?? {});
  const degraded = r?.degraded ?? [];
  const running = (jobs.data?.jobs ?? []).filter(
    (j) => j.status === 'running' || j.status === 'queued',
  ).length;

  const heroState = !r ? 'warn' : r.ready && degraded.length === 0 ? 'ok' : r.ready ? 'warn' : 'bad';
  const heroText = ready.error
    ? 'Readiness unknown'
    : !r
      ? 'Checking readiness…'
      : r.ready && degraded.length === 0
        ? 'All systems operational'
        : r.ready
          ? 'Operational — degraded'
          : 'Not ready';

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Dashboard</h1>
          <p className="page-sub">
            Live readiness of every pipeline dependency, and the scale of what this client knows.
          </p>
        </div>
        <div className="toolbar">
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              ready.reload();
              if (scoped) {
                docs.reload();
                facts.reload();
                jobs.reload();
              }
            }}
          >
            <IconRefresh size={13} /> Refresh
          </button>
        </div>
      </div>

      <div className="stack">
        {/* Readiness hero */}
        <section className="hero">
          <div className="hero-row">
            <div style={{ flex: 1, minWidth: 240 }}>
              <div className="hero-status">
                <span className={`hero-orb ${heroState}`} />
                {heroText}
              </div>
              <p className="hero-sub">
                {ready.error
                  ? `/readyz could not be read — ${ready.error.friendly}`
                  : !r
                    ? 'Contacting /readyz…'
                    : !r.ready
                      ? 'A required component is down. Ingest and retrieval will fail until it recovers — see the component detail below.'
                      : degraded.length > 0
                        ? `Serving traffic, but ${degraded.length} component${
                            degraded.length === 1 ? ' is' : 's are'
                          } degraded. Affected features fall back or fail closed.`
                        : 'Every dependency reported healthy. The full pipeline is available.'}
              </p>
            </div>
            <div className="row" style={{ gap: 6 }}>
              {components.map(([name, c]) => (
                <Badge key={name} tone={c.ok ? 'green' : 'red'} dot large>
                  {COMPONENT_LABEL[name] ?? name}
                </Badge>
              ))}
            </div>
          </div>
        </section>

        {/* Degraded callout */}
        {degraded.length > 0 && (
          <div className="banner amber">
            <IconAlert size={15} />
            <div>
              <div className="banner-title">What&rsquo;s degraded</div>
              <div className="banner-body">
                {degraded.map((d) => (
                  <span key={d}>
                    <code>{d}</code>{' '}
                  </span>
                ))}
                — see the component detail below for the exact cause and fallback.
              </div>
            </div>
          </div>
        )}

        {/* Counts */}
        <div className="grid grid-4">
          <Stat
            label="Documents"
            value={docs.data?.count ?? (scoped ? '—' : '·')}
            foot={scoped ? 'ingested for this client' : 'set a client to scope'}
            loading={docs.loading}
            to={scoped ? '/documents' : undefined}
          />
          <Stat
            label="Merged facts"
            value={facts.data?.count ?? (scoped ? '—' : '·')}
            foot="client-level resolved view"
            tone="gold"
            loading={facts.loading}
            to={scoped ? '/facts' : undefined}
          />
          <Stat
            label="Jobs"
            value={jobs.data?.jobs.length ?? (scoped ? '—' : '·')}
            foot="recent ingest jobs"
            loading={jobs.loading}
            to={scoped ? '/jobs' : undefined}
          />
          <Stat
            label="In flight"
            value={scoped ? running : '·'}
            foot="queued or running"
            tone={running > 0 ? 'amber' : 'green'}
            loading={jobs.loading}
            to={scoped ? '/jobs' : undefined}
          />
        </div>

        {!scoped && (
          <div className="banner info">
            <IconAlert size={15} />
            <div>
              <div className="banner-title">Scope not set</div>
              <div className="banner-body">
                Counts need both a client ID and an API key — set them in the header bar. Readiness
                above is unauthenticated and always available.
              </div>
            </div>
          </div>
        )}

        {/* Components */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">Components</div>
              <div className="card-sub">Reported by /readyz</div>
            </div>
            {r && (
              <Badge tone={r.ready ? 'green' : 'red'}>
                {r.ready ? 'ready' : 'not ready'}
              </Badge>
            )}
          </div>
          <div className="card-body">
            {ready.loading && !r && <SkeletonLines lines={4} />}
            {ready.error && <ErrorState error={ready.error} onRetry={ready.reload} />}
            {r && components.length === 0 && (
              <EmptyState title="No components reported" text="/readyz returned an empty component map." />
            )}
            {components.length > 0 && (
              <div className="grid grid-3">
                {components.map(([name, c]) => (
                  <ComponentCard key={name} name={name} comp={c} />
                ))}
              </div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
