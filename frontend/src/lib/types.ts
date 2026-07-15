/**
 * Wire types for the document-intelligence API (`/api/v1`).
 *
 * These mirror the server-side projections in `di/serving.py` and the enums in
 * `di/models.py`. Fields the server may omit are optional or nullable — the API
 * hands back raw row projections, so defensive typing here is deliberate.
 */

// --- Enums (mirrors di/models.py StrEnums) ---------------------------------

export type NodeType =
  | 'document'
  | 'section'
  | 'chunk'
  | 'table'
  | 'figure'
  | 'fact'
  | 'summary';

export type VerificationStatus =
  | 'checksum_verified'
  | 'gov_verified'
  | 'llm_unverified'
  | 'unverified';

export type SensitivityBucket = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';

export type GateDecision = 'SEND_TO_LLM' | 'REDACT_THEN_SEND' | 'DETERMINISTIC_ONLY';

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed';

/** Pipeline stages, in the order the stepper renders them. */
export const STAGES = ['ocr', 'gate', 'extract', 'subtree', 'arep', 'merge', 'done'] as const;
export type Stage = (typeof STAGES)[number];

// --- Provenance ------------------------------------------------------------

export interface BBox {
  x?: number;
  y?: number;
  w?: number;
  h?: number;
}

/** Provenance is a jsonb blob; known keys are typed, the rest passes through. */
export interface Provenance {
  page?: number | null;
  bbox?: BBox | number[] | null;
  extractor?: string | null;
  model?: string | null;
  ocr_engine?: string | null;
  ontology_version?: string | null;
  source?: string | null;
  [key: string]: unknown;
}

// --- Knowledge tree --------------------------------------------------------

export interface TreeNode {
  id: string;
  parent_id?: string | null;
  path: string | null;
  node_type: NodeType | string;
  seq?: number | null;
  depth?: number | null;
  title?: string | null;
  content?: string | null;
  context_prefix?: string | null;
  attribute_key?: string | null;
  value_text?: string | null;
  value_date?: string | null;
  value_num?: number | null;
  verification_status?: VerificationStatus | string | null;
  confidence?: number | null;
  sensitivity?: SensitivityBucket | string | null;
  valid_from?: string | null;
  valid_to?: string | null;
  provenance?: Provenance | null;
  doc_id?: string | null;
  version_id?: string | null;
  masked?: boolean;
  children: TreeNode[];
}

/** A search hit is a flat node projection carrying rank/score. */
export interface SearchHit extends Omit<TreeNode, 'children'> {
  children?: TreeNode[];
  _rank?: number;
  _score?: number;
}

export interface TreeResponse {
  client_id: string;
  count: number;
  tree: TreeNode[];
}

export interface SearchRequestBody {
  query: string;
  top_k?: number;
  mask?: boolean;
  scope_path?: string | null;
  doc_id?: string | null;
  current_only?: boolean;
}

export interface SearchResponse {
  client_id: string;
  query: string;
  count: number;
  hits: SearchHit[];
}

// --- Facts -----------------------------------------------------------------

export interface MergedFact {
  attribute_key: string;
  resolved_value?: string | null;
  value_date?: string | null;
  value_num?: number | null;
  confidence?: number | null;
  conflict?: boolean | null;
  needs_review?: boolean | null;
  verified?: boolean;
  masked?: boolean;
  sensitivity?: SensitivityBucket | string | null;
  source_fact_ids?: string[] | null;
  verification_status?: VerificationStatus | string | null;
}

export interface FactsResponse {
  client_id: string;
  count: number;
  facts: MergedFact[];
}

// --- Documents -------------------------------------------------------------

export interface DocumentRow {
  id: string;
  document_name?: string | null;
  doc_type?: string | null;
  doc_category?: string | null;
  jurisdiction?: string | null;
  sensitivity_bucket?: SensitivityBucket | string | null;
  gate_decision?: GateDecision | string | null;
  confidence?: number | null;
  ocr_engine?: string | null;
  page_count?: number | null;
  created_at?: string | null;
  external_document_id?: string | null;
}

export interface DocumentsResponse {
  client_id: string;
  count: number;
  documents: DocumentRow[];
  next_cursor?: string | null;
}

// --- Jobs ------------------------------------------------------------------

export interface JobEvent {
  stage: Stage | string;
  status: string;
  detail?: Record<string, unknown> | string | null;
  ts?: string | null;
}

export interface Job {
  id: string;
  client_id: string;
  status: JobStatus;
  stage?: Stage | string | null;
  document_name?: string | null;
  doc_id?: string | null;
  version_id?: string | null;
  error?: string | null;
  events?: JobEvent[];
  created_at?: string | null;
  updated_at?: string | null;
  finished_at?: string | null;
}

export interface JobsResponse {
  jobs: Job[];
  next_cursor?: string | null;
}

/** `POST /ingest` in job mode returns 202 with the accepted job handle. */
export interface IngestAccepted {
  job_id: string;
  status: JobStatus;
  client_id: string;
}

// --- Changes ---------------------------------------------------------------

export interface ChangeRow {
  id: string;
  doc_id: string;
  version_no?: number | null;
  content_hash?: string | null;
  is_current?: boolean | null;
  created_at?: string | null;
  document_name?: string | null;
  doc_type?: string | null;
  change_seq?: number | null;
}

export interface ChangesResponse {
  client_id: string;
  count: number;
  changes: ChangeRow[];
  next_cursor?: string | null;
}

// --- Manifest / answerable -------------------------------------------------

export interface Manifest {
  doc_id: string;
  document_name?: string | null;
  doc_type?: string | null;
  jurisdiction?: string | null;
  page_count?: number | null;
  languages?: string | null;
  sensitivity?: SensitivityBucket | string | null;
  gate_decision?: GateDecision | string | null;
  node_type_counts?: Record<string, number>;
  attribute_keys?: string[];
  verification_status_counts?: Record<string, number>;
  accessibility_rep_counts?: Record<string, number>;
  answerable?: boolean;
  searchable?: boolean;
}

export interface AnswerableQuestion {
  question?: string | null;
  knode_id?: string | null;
  path?: string | null;
  lang?: string | null;
}

export interface AnswerableResponse {
  client_id: string;
  doc_id: string;
  answerable: AnswerableQuestion[];
}

// --- Provenance lookup -----------------------------------------------------

export interface ProvenanceResponse {
  node_id: string;
  client_id?: string;
  doc_id?: string | null;
  version_id?: string | null;
  node_type?: NodeType | string | null;
  attribute_key?: string | null;
  verification_status?: VerificationStatus | string | null;
  confidence?: number | null;
  provenance?: Provenance | null;
}

// --- Health / readiness ----------------------------------------------------

export interface ReadyComponent {
  ok: boolean;
  detail?: string | null;
  extra?: Record<string, unknown> | null;
}

export interface ReadyzResponse {
  ready: boolean;
  degraded: string[];
  components: Record<string, ReadyComponent>;
}

export interface HealthResponse {
  status: string;
  service: string;
}

// --- Deletes ---------------------------------------------------------------

export interface DeleteResponse {
  deleted: Record<string, unknown>;
}
