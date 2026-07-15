/**
 * Typed API client for the document-intelligence backend.
 *
 * Every `/api/v1` call carries the `X-API-KEY` header taken from the key the
 * operator pastes into the header bar (persisted in localStorage). Non-2xx
 * responses become an `ApiError` carrying the HTTP status and the server's
 * `detail`, so callers can branch on 401 (bad/missing key) and 403 (no access
 * to that client) instead of failing silently.
 */
import type {
  AnswerableResponse,
  ChangesResponse,
  DeleteResponse,
  DocumentsResponse,
  FactsResponse,
  HealthResponse,
  IngestAccepted,
  Job,
  JobsResponse,
  JobStatus,
  Manifest,
  ProvenanceResponse,
  ReadyzResponse,
  SearchRequestBody,
  SearchResponse,
  TreeResponse,
} from './types';

const API_ROOT = '/api/v1';

/** Error thrown for any non-2xx response, carrying status + server detail. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly body: unknown;

  constructor(status: number, detail: string, body?: unknown) {
    super(detail || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.body = body;
  }

  /** Missing or rejected API key. */
  get isAuth(): boolean {
    return this.status === 401;
  }

  /** Authenticated, but the key has no access to this client / lacks scope. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }

  /** A human-facing, actionable message for the error banners. */
  get friendly(): string {
    if (this.isAuth) {
      return 'Unauthorized — the API key is missing or not accepted. Paste a valid key in the header bar.';
    }
    if (this.isForbidden) {
      return `Forbidden — this API key has no access to that client${
        this.detail ? ` (${this.detail})` : ''
      }.`;
    }
    if (this.status === 0) {
      return 'Cannot reach the API. Is the backend running?';
    }
    return this.detail || `Request failed with HTTP ${this.status}`;
  }
}

/** Pull the most useful message out of a FastAPI error body. */
function extractDetail(body: unknown, status: number): string {
  if (typeof body === 'string' && body.trim()) return body.trim();
  if (body && typeof body === 'object') {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === 'string') return detail;
    // FastAPI validation errors: detail is a list of {loc, msg, type}
    if (Array.isArray(detail)) {
      const msgs = detail
        .map((d) => {
          if (d && typeof d === 'object') {
            const item = d as { msg?: unknown; loc?: unknown };
            const loc = Array.isArray(item.loc) ? item.loc.join('.') : '';
            const msg = typeof item.msg === 'string' ? item.msg : '';
            return loc ? `${loc}: ${msg}` : msg;
          }
          return String(d);
        })
        .filter(Boolean);
      if (msgs.length) return msgs.join('; ');
    }
    const message = (body as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  }
  return `HTTP ${status}`;
}

async function parseBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

/**
 * True when a 2xx body is not the JSON object the contract promises.
 *
 * This is the SPA-fallback trap: FastAPI serves `index.html` for any unknown
 * non-`/api` path, so calling an endpoint that does not exist yet returns
 * **200 text/html**, not a 404. Parsing that as the expected payload yields
 * `undefined` fields and a silently wrong screen, so we reject it loudly.
 */
function isNotJsonObject(body: unknown): boolean {
  return body === null || typeof body !== 'object' || Array.isArray(body);
}

/** localStorage key holding the operator's API key. Owned here, used by the settings provider. */
export const API_KEY_STORAGE = 'di.apiKey';

/**
 * Default: read straight from localStorage.
 *
 * This must not depend on the React tree having mounted. Child effects run
 * BEFORE parent effects, so a page's first fetch fires before the provider's
 * effect could register a getter — with a stub getter that would send no
 * `X-API-KEY` at all and get a spurious 401 on every hard reload. Reading
 * storage directly makes the key available from the very first request.
 */
let apiKeyGetter: () => string = () => {
  try {
    return localStorage.getItem(API_KEY_STORAGE) ?? '';
  } catch {
    return '';
  }
};

/**
 * Override the source of the API key. The provider registers the in-memory
 * value so edits in the header bar apply without a round-trip to storage.
 */
export function setApiKeyGetter(fn: () => string): void {
  apiKeyGetter = fn;
}

function authHeaders(): Record<string, string> {
  const key = apiKeyGetter();
  return key ? { 'X-API-KEY': key } : {};
}

interface RequestOptions {
  method?: string;
  body?: BodyInit | null;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** Skip the X-API-KEY header (for the unauthenticated /health, /readyz). */
  anonymous?: boolean;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body = null, headers = {}, signal, anonymous = false } = opts;
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      body,
      signal,
      headers: { Accept: 'application/json', ...(anonymous ? {} : authHeaders()), ...headers },
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new ApiError(0, err instanceof Error ? err.message : 'Network request failed');
  }

  const parsed = await parseBody(res);
  if (!res.ok) throw new ApiError(res.status, extractDetail(parsed, res.status), parsed);

  if (isNotJsonObject(parsed)) {
    const ct = res.headers.get('content-type') ?? 'unknown';
    const looksLikeSpaFallback = ct.includes('text/html');
    throw new ApiError(
      res.status,
      looksLikeSpaFallback
        ? `${path} returned the console's HTML shell instead of JSON — that endpoint is not implemented on this backend.`
        : `${path} returned ${ct} instead of a JSON object.`,
      parsed,
    );
  }
  return parsed as T;
}

function json<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  return request<T>(path, opts);
}

function postJson<T>(path: string, payload: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    body: JSON.stringify(payload),
    headers: { 'Content-Type': 'application/json' },
    signal,
  });
}

/** Build a query string, dropping undefined/null/empty values. */
function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    search.set(k, String(v));
  }
  const s = search.toString();
  return s ? `?${s}` : '';
}

const enc = encodeURIComponent;

// --- Health / readiness (unauthenticated) ----------------------------------

export function getReadyz(signal?: AbortSignal): Promise<ReadyzResponse> {
  return json<ReadyzResponse>('/readyz', { anonymous: true, signal });
}

export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return json<HealthResponse>('/health', { anonymous: true, signal });
}

// --- Ingest ----------------------------------------------------------------

export interface IngestArgs {
  clientId: string;
  file: File;
  externalDocumentId?: string;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

/** Upload a document. Returns the accepted job handle (202). */
export function ingest({
  clientId,
  file,
  externalDocumentId,
  idempotencyKey,
  signal,
}: IngestArgs): Promise<IngestAccepted> {
  const form = new FormData();
  form.set('client_id', clientId);
  form.set('file', file, file.name);
  if (externalDocumentId) form.set('external_document_id', externalDocumentId);
  if (idempotencyKey) form.set('idempotency_key', idempotencyKey);
  // NOTE: no Content-Type header — the browser sets the multipart boundary.
  return request<IngestAccepted>(`${API_ROOT}/ingest`, { method: 'POST', body: form, signal });
}

// --- Jobs ------------------------------------------------------------------

export function getJob(jobId: string, clientId: string, signal?: AbortSignal): Promise<Job> {
  return json<Job>(`${API_ROOT}/jobs/${enc(jobId)}${qs({ client_id: clientId })}`, { signal });
}

export interface ListJobsArgs {
  clientId: string;
  limit?: number;
  cursor?: string | null;
  status?: JobStatus | null;
  signal?: AbortSignal;
}

export function listJobs({
  clientId,
  limit,
  cursor,
  status,
  signal,
}: ListJobsArgs): Promise<JobsResponse> {
  return json<JobsResponse>(
    `${API_ROOT}/jobs${qs({ client_id: clientId, limit, cursor, status })}`,
    { signal },
  );
}

// --- Documents -------------------------------------------------------------

export interface ListDocumentsArgs {
  clientId: string;
  limit?: number;
  cursor?: string | null;
  signal?: AbortSignal;
}

export function listDocuments({
  clientId,
  limit,
  cursor,
  signal,
}: ListDocumentsArgs): Promise<DocumentsResponse> {
  return json<DocumentsResponse>(
    `${API_ROOT}/clients/${enc(clientId)}/documents${qs({ limit, cursor })}`,
    { signal },
  );
}

export function deleteDocument(
  clientId: string,
  docId: string,
  signal?: AbortSignal,
): Promise<DeleteResponse> {
  return json<DeleteResponse>(`${API_ROOT}/clients/${enc(clientId)}/documents/${enc(docId)}`, {
    method: 'DELETE',
    signal,
  });
}

/** Purge every trace of a client. Requires admin scope on the API key. */
export function purgeClient(clientId: string, signal?: AbortSignal): Promise<DeleteResponse> {
  return json<DeleteResponse>(`${API_ROOT}/clients/${enc(clientId)}`, {
    method: 'DELETE',
    signal,
  });
}

// --- Tree ------------------------------------------------------------------

export interface TreeArgs {
  clientId: string;
  docId?: string | null;
  path?: string | null;
  maxDepth?: number | null;
  mask?: boolean;
  signal?: AbortSignal;
}

export function getTree({
  clientId,
  docId,
  path,
  maxDepth,
  mask,
  signal,
}: TreeArgs): Promise<TreeResponse> {
  return json<TreeResponse>(
    `${API_ROOT}/clients/${enc(clientId)}/tree${qs({
      doc_id: docId,
      path,
      max_depth: maxDepth,
      mask,
    })}`,
    { signal },
  );
}

// --- Facts -----------------------------------------------------------------

export interface FactsArgs {
  clientId: string;
  attributeKey?: string | null;
  verifiedOnly?: boolean;
  mask?: boolean;
  signal?: AbortSignal;
}

export function getFacts({
  clientId,
  attributeKey,
  verifiedOnly,
  mask,
  signal,
}: FactsArgs): Promise<FactsResponse> {
  return json<FactsResponse>(
    `${API_ROOT}/clients/${enc(clientId)}/facts${qs({
      attribute_key: attributeKey,
      verified_only: verifiedOnly,
      mask,
    })}`,
    { signal },
  );
}

// --- Search ----------------------------------------------------------------

export function search(
  clientId: string,
  body: SearchRequestBody,
  signal?: AbortSignal,
): Promise<SearchResponse> {
  return postJson<SearchResponse>(`${API_ROOT}/clients/${enc(clientId)}/search`, body, signal);
}

// --- Provenance ------------------------------------------------------------

export function getProvenance(
  nodeId: string,
  clientId: string,
  signal?: AbortSignal,
): Promise<ProvenanceResponse> {
  return json<ProvenanceResponse>(
    `${API_ROOT}/nodes/${enc(nodeId)}/provenance${qs({ client_id: clientId })}`,
    { signal },
  );
}

// --- Changes ---------------------------------------------------------------

export interface ChangesArgs {
  clientId: string;
  since?: string | null;
  cursor?: string | null;
  signal?: AbortSignal;
}

export function getChanges({
  clientId,
  since,
  cursor,
  signal,
}: ChangesArgs): Promise<ChangesResponse> {
  return json<ChangesResponse>(
    `${API_ROOT}/clients/${enc(clientId)}/changes${qs({ since, cursor })}`,
    { signal },
  );
}

// --- Manifest / answerable -------------------------------------------------

export function getManifest(
  clientId: string,
  docId: string,
  signal?: AbortSignal,
): Promise<Manifest> {
  return json<Manifest>(`${API_ROOT}/clients/${enc(clientId)}/docs/${enc(docId)}/manifest`, {
    signal,
  });
}

export function getAnswerable(
  clientId: string,
  docId: string,
  signal?: AbortSignal,
): Promise<AnswerableResponse> {
  return json<AnswerableResponse>(
    `${API_ROOT}/clients/${enc(clientId)}/docs/${enc(docId)}/answerable`,
    { signal },
  );
}
