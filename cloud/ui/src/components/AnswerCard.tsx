import { useState } from "react";

import { formatCell, formatMs, highlightSql, renderAnswer } from "../lib/render";
import type { AskResponse, Route, SourceRef, Timings } from "../types";

type Tab = "answer" | "sources" | "sql" | "data" | "trace";

export const ROUTE_LABEL: Record<Route, string> = {
  document_rag: "Documents",
  text_to_sql: "Database",
  multi_source: "Documents + database",
  refuse: "Refused",
  clarify: "Needs clarification",
};

/** Route colour carries meaning: a refusal should not look like an answer. */
const ROUTE_TONE: Record<Route, string> = {
  document_rag: "badge--accent",
  text_to_sql: "badge--info",
  multi_source: "badge--accent",
  refuse: "badge--danger",
  clarify: "badge--warn",
};

export const STATUS_TONE: Record<string, string> = {
  answered: "badge--ok",
  partial: "badge--warn",
  insufficient_evidence: "badge--warn",
  conflicting_sources: "badge--warn",
  refused: "badge--danger",
  clarification_needed: "badge--warn",
};

export const STATUS_LABEL: Record<string, string> = {
  answered: "Answered",
  partial: "Partially answered",
  insufficient_evidence: "Not enough evidence",
  conflicting_sources: "Sources conflict",
  refused: "Refused",
  clarification_needed: "Clarification needed",
};

export default function AnswerCard({ result }: { result: AskResponse }) {
  const [tab, setTab] = useState<Tab>("answer");

  const documents = result.sources.filter((s) => s.type !== "sql_result");
  const hasSql = Boolean(result.sql?.generated);
  const hasRows = result.rows.length > 0;

  const tabs: Array<{ id: Tab; label: string; count?: number; show: boolean }> = [
    { id: "answer", label: "Answer", show: true },
    { id: "sources", label: "Sources", count: documents.length, show: documents.length > 0 },
    { id: "sql", label: "SQL", show: hasSql },
    { id: "data", label: "Data", count: result.row_count, show: hasRows },
    { id: "trace", label: "Trace", show: true },
  ];

  return (
    <article className="card">
      <header className="card__head">
        <span className={`badge ${ROUTE_TONE[result.route]}`}>
          <span className="dot" />
          {ROUTE_LABEL[result.route]}
        </span>
        <span className={`badge ${STATUS_TONE[result.status] ?? "badge--neutral"}`}>
          {STATUS_LABEL[result.status] ?? result.status}
        </span>
        {result.grounded ? (
          <span className="badge badge--neutral" title="Every citation in the answer resolves to evidence that was actually retrieved.">
            Grounded
          </span>
        ) : (
          <span className="badge badge--warn" title="The answer cites nothing, or cites something that was not retrieved. Treat it as unverified.">
            Ungrounded
          </span>
        )}
        <span className="card__spacer" />
        <span className="badge badge--neutral" title="End-to-end latency">
          {formatMs(result.timings_ms.total)}
        </span>
      </header>

      <div className="tabs" role="tablist">
        {tabs
          .filter((t) => t.show)
          .map((t) => (
            <button
              key={t.id}
              role="tab"
              className="tab"
              aria-selected={tab === t.id}
              onClick={() => setTab(t.id)}
            >
              {t.label}
              {t.count !== undefined && <span className="tab__count">{t.count}</span>}
            </button>
          ))}
      </div>

      {tab === "answer" && <AnswerPane result={result} />}
      {tab === "sources" && <SourcesPane sources={documents} />}
      {tab === "sql" && <SqlPane result={result} />}
      {tab === "data" && <DataPane result={result} />}
      {tab === "trace" && <TracePane result={result} />}
    </article>
  );
}

// ---------------------------------------------------------------------------

function AnswerPane({ result }: { result: AskResponse }) {
  return (
    <div className="pane">
      {result.conflicts.map((conflict, i) => (
        <div key={i} className="notice notice--warn">
          <span className="notice__icon">!</span>
          <span>{conflict}</span>
        </div>
      ))}
      {result.notes.map((note, i) => (
        <div key={i} className="notice notice--info">
          <span className="notice__icon">i</span>
          <span>{note}</span>
        </div>
      ))}
      <div className="answer">{renderAnswer(result.answer, result.sources)}</div>
    </div>
  );
}

function SourcesPane({ sources }: { sources: SourceRef[] }) {
  if (!sources.length) return <div className="pane empty">No documents were retrieved.</div>;

  return (
    <div className="pane">
      {sources.map((source) => (
        <div key={source.id} className="source">
          <span className="source__tag">{source.id}</span>
          <div className="source__body">
            <div className="source__title">{source.title || "(untitled)"}</div>
            <div className="source__meta">
              {source.version && <span>v{source.version}</span>}
              {source.section && <span>· {source.section}</span>}
              {source.effective_date && <span>· effective {source.effective_date}</span>}
              {source.authority && <span>· {source.authority}</span>}
              {source.retrieval_method && <span>· {source.retrieval_method}</span>}
              {source.score !== null && <span>· score {source.score.toFixed(3)}</span>}
            </div>
            {source.snippet && <div className="source__snippet">{source.snippet}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

export function SqlPane({ result }: { result: AskResponse }) {
  const sql = result.sql;
  if (!sql) return <div className="pane empty">No query was generated.</div>;

  return (
    <div className="pane">
      {sql.blocked && (
        <div className="notice notice--danger">
          <span className="notice__icon">✕</span>
          <span>
            <strong>Blocked by the SQL guard.</strong> {sql.block_reason}
          </span>
        </div>
      )}
      {sql.tenant_injected && (
        <div className="notice notice--warn">
          <span className="notice__icon">!</span>
          <span>
            The model omitted the tenant filter. It was added automatically before execution, so
            the result covers only your own tenant.
          </span>
        </div>
      )}
      {sql.warnings.map((warning, i) => (
        <div key={i} className="notice notice--info">
          <span className="notice__icon">i</span>
          <span>{warning}</span>
        </div>
      ))}

      <pre className="sql-block">{highlightSql(sql.executed ?? sql.generated ?? "")}</pre>

      <dl className="kv">
        <dt>Provider</dt>
        <dd>{sql.provider}</dd>
        <dt>Tables read</dt>
        <dd>{sql.tables.length ? sql.tables.join(", ") : "—"}</dd>
        <dt>Joins</dt>
        <dd>{sql.join_count}</dd>
        {sql.executed && sql.generated && sql.executed !== sql.generated && (
          <>
            <dt>As generated</dt>
            <dd style={{ whiteSpace: "pre-wrap" }}>{sql.generated}</dd>
          </>
        )}
      </dl>
    </div>
  );
}

export function DataPane({ result }: { result: AskResponse }) {
  if (!result.rows.length) return <div className="pane empty">The query returned no rows.</div>;

  // Showing everything would make a 5,000-row result unusable and slow. The
  // count in the header stays truthful about how many there are.
  const shown = result.rows.slice(0, 200);

  return (
    <div className="pane">
      {result.truncated && (
        <div className="notice notice--info">
          <span className="notice__icon">i</span>
          <span>
            The result hit the server-side row cap. Narrow the question to see the rest.
          </span>
        </div>
      )}
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              {result.columns.map((column) => (
                <th key={column}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, i) => (
              <tr key={i}>
                {result.columns.map((column) => {
                  const cell = formatCell(row[column]);
                  return (
                    <td key={column} className={cell.numeric ? "num" : undefined}>
                      {cell.text}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {result.rows.length > shown.length && (
        <div className="empty">
          Showing the first {shown.length} of {result.row_count.toLocaleString()} rows.
        </div>
      )}
    </div>
  );
}

const STAGE_LABEL: Record<keyof Timings, string> = {
  routing: "Routing",
  retrieval: "Retrieval",
  sql_generation: "SQL generation",
  sql_execution: "SQL execution",
  generation: "Answer generation",
  total: "Total",
};

export function TracePane({ result }: { result: AskResponse }) {
  const stages = (Object.keys(STAGE_LABEL) as Array<keyof Timings>)
    .filter((key) => key !== "total" && typeof result.timings_ms[key] === "number")
    .map((key) => ({ key, ms: result.timings_ms[key] as number }));

  const longest = Math.max(1, ...stages.map((s) => s.ms));

  return (
    <div className="pane">
      {stages.map((stage) => (
        <div key={stage.key} className="trace-row">
          <span className="trace-row__name">{STAGE_LABEL[stage.key]}</span>
          <span className="trace-row__track">
            <span
              className="trace-row__bar"
              style={{ width: `${Math.max(2, (stage.ms / longest) * 100)}%` }}
            />
          </span>
          <span className="trace-row__ms">{formatMs(stage.ms)}</span>
        </div>
      ))}

      <dl className="kv">
        <dt>Trace id</dt>
        <dd>{result.trace_id}</dd>
        <dt>Route decided by</dt>
        <dd>
          {result.route_decided_by}
          {result.route_confidence ? ` (${(result.route_confidence * 100).toFixed(0)}%)` : ""}
        </dd>
        <dt>Chat model</dt>
        <dd>{result.versions.chat_model}</dd>
        <dt>SQL provider</dt>
        <dd>{result.versions.sql_provider}</dd>
        <dt>Prompt version</dt>
        <dd>{result.versions.prompt_version}</dd>
        <dt>Index version</dt>
        <dd>{result.versions.index_version}</dd>
      </dl>

      {result.warnings.length > 0 && (
        <div style={{ marginTop: 14 }}>
          {result.warnings.map((warning, i) => (
            <div key={i} className="notice notice--warn">
              <span className="notice__icon">!</span>
              <span>{warning}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
