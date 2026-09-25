/**
 * The wire contract with the backend.
 *
 * These mirror `enterprise_copilot.models.evidence` and `CopilotTrace`. They
 * are written by hand rather than generated because the backend deliberately
 * sends a narrowed view: the full evidence package carries chunk text and
 * permission metadata that the browser has no business holding.
 */

export type Route =
  | "document_rag"
  | "text_to_sql"
  | "multi_source"
  | "refuse"
  | "clarify";

export type AnswerStatus =
  | "answered"
  | "partial"
  | "insufficient_evidence"
  | "conflicting_sources"
  | "refused"
  | "clarification_needed"
  | "direct";

export type Strategy = "dense" | "sparse" | "hybrid" | "reranked";

export interface SourceRef {
  /** Citation label as it appears in the answer text, e.g. "D1" or "S1". */
  id: string;
  type: "document" | "sql_result" | "glossary";
  title: string;
  version: string;
  section: string;
  effective_date: string;
  authority: string;
  score: number | null;
  rerank_score: number | null;
  retrieval_method: string;
  snippet: string;
}

export interface SqlDetail {
  generated: string | null;
  executed: string | null;
  blocked: boolean;
  block_reason: string | null;
  tenant_injected: boolean;
  tables: string[];
  join_count: number;
  warnings: string[];
  provider: string;
}

export interface Timings {
  routing?: number;
  retrieval?: number;
  sql_generation?: number;
  sql_execution?: number;
  generation?: number;
  total: number;
}

export interface AskResponse {
  trace_id: string;
  question: string;
  route: Route;
  route_decided_by: string;
  route_confidence: number;
  status: AnswerStatus;
  answer: string;
  grounded: boolean;
  sources: SourceRef[];
  conflicts: string[];
  notes: string[];
  warnings: string[];
  sql: SqlDetail | null;
  columns: string[];
  rows: Array<Record<string, unknown>>;
  row_count: number;
  truncated: boolean;
  timings_ms: Timings;
  versions: {
    chat_model: string;
    prompt_version: string;
    index_version: string;
    sql_provider: string;
  };
}

export interface Persona {
  key: string;
  label: string;
  tenant: string;
  tenant_id: number;
  access_groups: string[];
  description: string;
}

export interface MetaResponse {
  app_name: string;
  /** Which runtime answers: drives the LOCAL / CLOUD badge. */
  deployment_mode: "local" | "cloud";
  llm_provider: string;
  embedding_provider: string;
  embedding_model: string;
  sql_enabled: boolean;
  upload_enabled: boolean;
  max_upload_mb: number;
  personas: Persona[];
  strategies: Strategy[];
  default_strategy: Strategy;
  sql_provider: string;
  chat_model: string;
  index_version: string;
  document_count: number;
  chunk_count: number;
  example_questions: string[];
}

export interface HealthResponse {
  status: "ok" | "degraded" | "down";
  checks: Array<{ name: string; ok: boolean; detail: string }>;
}

export interface ApiError {
  error: string;
  detail: string;
}

// --- retrieval debugger ----------------------------------------------------

export interface ScoredChunk {
  rank: number;
  doc_id: string;
  title: string;
  version: string;
  section: string;
  score: number;
  dense_score: number | null;
  dense_rank: number | null;
  sparse_score: number | null;
  sparse_rank: number | null;
  rerank_score: number | null;
  snippet: string;
}

export interface StrategyRun {
  strategy: Strategy;
  elapsed_ms: number;
  results: ScoredChunk[];
  error: string | null;
}

export interface RetrieveResponse {
  query: string;
  runs: StrategyRun[];
}

// --- evaluation ------------------------------------------------------------

export interface StrategyScore {
  name: Strategy;
  ndcg: number;
  /** 95% bootstrap confidence interval, [low, high]. */
  ci: [number, number];
  mrr: number;
  recall: number;
  median_ms: number;
}

export interface Comparison {
  pair: string;
  delta: number;
  ci: [number, number];
  p: number;
  significant: boolean;
}

export interface EvaluationReport {
  baseline_id: string;
  generated_at_utc: string;
  source_artifact: string;
  cases: number;
  k: number;
  bootstrap_resamples: number;
  strategies: StrategyScore[];
  comparisons: Comparison[];
  headline: string;
  current: boolean;
  stale_reasons: string[];
  evaluated_embedding_provider: string;
  evaluated_embedding_model: string;
  evaluated_index_version: string;
  live_embedding_provider: string;
  live_embedding_model: string;
  live_index_version: string;
}

export interface EvaluationResponse {
  runs: Array<{ file: string; kind: string; payload: unknown }>;
  report: EvaluationReport;
}

// --- corpus ----------------------------------------------------------------

export interface DocumentSummary {
  doc_id: string;
  title: string;
  doc_type: string;
  version: string;
  effective_date: string;
  status: string;
  authority: string;
  department: string;
  owner: string;
  supersedes: string | null;
  superseded_by: string | null;
  related_docs: string[];
  tags: string[];
  words: number;
  readable: boolean;
  source?: "corpus" | "upload";
}

export interface DocumentsResponse {
  documents: DocumentSummary[];
  total: number;
  /** How many the access model withheld. The point of the screen, not a footnote. */
  hidden_by_permissions: number;
  persona: string;
}

export interface UploadResponse {
  doc_id: string;
  title: string;
  chunks_written: number;
  chunk_count: number;
}

/** Server-sent events from POST /ask/stream, in order. */
export type StreamEvent =
  | { type: "sources"; trace_id: string; route: Route | ""; sources: SourceRef[] }
  | { type: "token"; text: string }
  | { type: "done"; result: AskResponse }
  | { type: "error"; error: string; detail: string };
