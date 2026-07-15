/** Small presentation helpers shared across pages. */

/** Human-readable relative/absolute timestamp. Returns an em-dash for empty input. */
export function formatTs(ts: string | null | undefined): string {
  if (!ts) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/** Compact "3m ago" style age. */
export function formatAgo(ts: string | null | undefined): string {
  if (!ts) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  const secs = Math.floor((Date.now() - d.getTime()) / 1000);
  if (secs < 0) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return d.toLocaleDateString();
}

/** Percentage string for a 0..1 confidence. */
export function formatPct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${Math.round(v * 100)}%`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Shorten a UUID for dense table cells. */
export function shortId(id: string | null | undefined): string {
  if (!id) return '—';
  return id.length > 12 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}

/** Turn `id.curp` / `SEND_TO_LLM` into readable label text. */
export function humanize(value: string | null | undefined): string {
  if (!value) return '—';
  return value.replace(/[._]/g, ' ').toLowerCase();
}

/**
 * Render a job event's `detail` (an arbitrary jsonb blob) as compact
 * `key: value` pairs for the stepper.
 */
export function detailPairs(
  detail: Record<string, unknown> | string | null | undefined,
): Array<[string, string]> {
  if (!detail) return [];
  if (typeof detail === 'string') return [['info', detail]];
  return Object.entries(detail)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => {
      let text: string;
      if (Array.isArray(v)) text = v.length <= 4 ? v.join(', ') : `${v.length} items`;
      else if (typeof v === 'object') text = JSON.stringify(v);
      else if (typeof v === 'number') text = Number.isInteger(v) ? String(v) : v.toFixed(2);
      else text = String(v);
      return [k.replace(/_/g, ' '), text] as [string, string];
    });
}

/**
 * Choose a precision that does not destroy the value.
 *
 * Normalised coordinates (0..1) need real decimals — `toFixed(1)` would render
 * a height of 0.05 as "0.1", doubling it. Absolute pixel coordinates only need
 * one. This is audit-trail provenance, so the displayed number must match the
 * stored one.
 */
function formatCoord(n: number, normalised: boolean): string {
  return normalised ? String(Number(n.toFixed(4))) : n.toFixed(1);
}

function looksNormalised(nums: number[]): boolean {
  return nums.length > 0 && nums.every((n) => Math.abs(n) <= 1);
}

/** Bounding box → readable coordinates, tolerating both object and array shapes. */
export function formatBBox(bbox: unknown): string | null {
  if (!bbox) return null;
  if (Array.isArray(bbox)) {
    const nums = bbox.filter((n): n is number => typeof n === 'number');
    if (!nums.length) return null;
    const norm = looksNormalised(nums);
    return nums.map((n) => formatCoord(n, norm)).join(', ');
  }
  if (typeof bbox === 'object') {
    const b = bbox as Record<string, unknown>;
    const keys = ['x', 'y', 'w', 'h'] as const;
    const nums = keys.map((k) => b[k]).filter((n): n is number => typeof n === 'number');
    if (!nums.length) return JSON.stringify(bbox);
    const norm = looksNormalised(nums);
    return keys
      .filter((k) => typeof b[k] === 'number')
      .map((k) => `${k} ${formatCoord(b[k] as number, norm)}`)
      .join(' · ');
  }
  return String(bbox);
}
