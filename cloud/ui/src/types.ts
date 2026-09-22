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
  | "clarification_needed";

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
