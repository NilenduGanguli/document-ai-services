/**
 * Provenance drawer — every answer is one hop from its source page / bbox /
 * extractor / model / confidence.
 */
import type { ReactNode } from 'react';
import { getProvenance } from '../lib/api';
import { formatBBox } from '../lib/format';
import { useAsync } from '../hooks/useAsync';
import type { Provenance } from '../lib/types';
import { Badge, ConfidenceBar, VerificationBadge } from './Badge';
import { Drawer } from './Drawer';
import { ErrorState, SkeletonLines } from './States';

/** Keys rendered as first-class rows; everything else falls into the raw blob. */
const KNOWN = new Set(['page', 'bbox', 'extractor', 'model', 'ocr_engine', 'ontology_version', 'source']);

function normaliseBox(bbox: unknown): { x: number; y: number; w: number; h: number } | null {
  if (!bbox) return null;
  if (Array.isArray(bbox) && bbox.length >= 4) {
    const [x, y, w, h] = bbox as number[];
    if ([x, y, w, h].every((n) => typeof n === 'number')) {
      return { x: x as number, y: y as number, w: w as number, h: h as number };
    }
    return null;
  }
  if (typeof bbox === 'object') {
    const b = bbox as Record<string, unknown>;
    const nums = ['x', 'y', 'w', 'h'].map((k) => b[k]);
    if (nums.every((n) => typeof n === 'number')) {
      const [x, y, w, h] = nums as number[];
      return { x: x as number, y: y as number, w: w as number, h: h as number };
    }
  }
  return null;
}

/**
 * Schematic page mini-map. Coordinates are assumed normalised (0..1); values
 * that look like absolute pixels are scaled down by a nominal page width so the
 * marker still lands somewhere sensible rather than off-canvas.
 */
function BBoxMap({ bbox }: { bbox: unknown }): JSX.Element | null {
  const box = normaliseBox(bbox);
  if (!box) return null;
  const normalised = box.x <= 1 && box.y <= 1 && box.w <= 1 && box.h <= 1;
  const sx = normalised ? 1 : 1 / 1000;
  const sy = normalised ? 1 : 1 / 1400;
  const clamp = (n: number) => Math.max(0, Math.min(1, n));
  return (
    <div className="bbox-map" role="img" aria-label="Approximate location of the value on the page">
      <span
        className="bbox-map-rect"
        style={{
          left: `${clamp(box.x * sx) * 100}%`,
          top: `${clamp(box.y * sy) * 100}%`,
          width: `${clamp(box.w * sx) * 100}%`,
          height: `${clamp(box.h * sy) * 100}%`,
        }}
      />
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <>
      <span className="prov-key">{label}</span>
      <span className="prov-val">{children}</span>
    </>
  );
}

export function ProvenanceDrawer({
  nodeId,
  clientId,
  onClose,
  titleHint,
}: {
  nodeId: string | null;
  clientId: string;
  onClose: () => void;
  titleHint?: string | null;
}): JSX.Element | null {
  const { data, error, loading, reload } = useAsync(
    (signal) => getProvenance(nodeId as string, clientId, signal),
    [nodeId, clientId],
    !!nodeId && !!clientId,
  );

  if (!nodeId) return null;

  const prov: Provenance = data?.provenance ?? {};
  const extras = Object.entries(prov).filter(([k]) => !KNOWN.has(k));

  return (
    <Drawer
      open
      onClose={onClose}
      title={titleHint || 'Provenance'}
      subtitle={<span className="mono">{nodeId}</span>}
    >
      {loading && <SkeletonLines lines={6} />}
      {error && <ErrorState error={error} onRetry={reload} />}

      {data && !loading && (
        <>
          <div className="section-label">Grounding</div>
          <div className="prov-grid">
            <Row label="Page">{prov.page ?? '—'}</Row>
            <Row label="BBox">{formatBBox(prov.bbox) ?? '—'}</Row>
            <Row label="Extractor">{prov.extractor ?? '—'}</Row>
            <Row label="Model">{prov.model ?? '—'}</Row>
            <Row label="OCR engine">{prov.ocr_engine ?? '—'}</Row>
            <Row label="Ontology">{prov.ontology_version ?? '—'}</Row>
          </div>

          {normaliseBox(prov.bbox) && (
            <>
              <div className="section-label">Location on page {prov.page ?? ''}</div>
              <BBoxMap bbox={prov.bbox} />
            </>
          )}

          <div className="section-label">Assertion</div>
          <div className="prov-grid">
            <Row label="Node type">
              <Badge tone="neutral">{data.node_type ?? '—'}</Badge>
            </Row>
            <Row label="Attribute">{data.attribute_key ?? '—'}</Row>
            <Row label="Verification">
              <VerificationBadge value={data.verification_status} />
            </Row>
            <Row label="Confidence">
              {data.confidence === null || data.confidence === undefined ? (
                '—'
              ) : (
                <ConfidenceBar value={data.confidence} />
              )}
            </Row>
          </div>

          <div className="section-label">Source</div>
          <div className="prov-grid">
            <Row label="Document">{data.doc_id ?? '—'}</Row>
            <Row label="Version">{data.version_id ?? '—'}</Row>
          </div>

          {extras.length > 0 && (
            <>
              <div className="section-label">Raw provenance</div>
              <pre className="json">{JSON.stringify(prov, null, 2)}</pre>
            </>
          )}
        </>
      )}
    </Drawer>
  );
}
