import { useState } from "react";

import { formatMs, renderAnswer } from "../lib/render";
import type { AskResponse, Route, SourceRef } from "../types";
import { DataPane, ROUTE_LABEL, SqlPane, STATUS_LABEL, TracePane } from "./AnswerCard";

export interface Turn {
  id: string;
  question: string;
  /** Answer text so far; the final validated text once `phase` is done. */
  text: string;
  phase: "searching" | "writing" | "done" | "error" | "stopped";
  route?: Route | "";
  sources?: SourceRef[];
  result?: AskResponse;
  error?: string;
}

/** Statuses that should read as a caution, not as a normal answer. */
const CAUTION = new Set(["insufficient_evidence", "partial", "conflicting_sources", "clarification_needed"]);

export default function ChatMessage({ turn }: { turn: Turn }) {
  return (
    <div className="turn">
      <div className="msg msg-user">
        <div className="user-bubble">{turn.question}</div>
      </div>
      <AssistantMessage turn={turn} />
    </div>
  );
}

function AssistantMessage({ turn }: { turn: Turn }) {
  const [details, setDetails] = useState(false);
  const sources = (turn.result?.sources ?? turn.sources ?? []).filter((s) => s.type !== "sql_result");
  const result = turn.result;
  const status = result?.status;

  return (
    <div className="msg msg-assistant">
      <div className="avatar" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="14" height="14">
          <path d="M12 2 3 7v10l9 5 9-5V7l-9-5Zm0 2.3 6.8 3.8L12 11.9 5.2 8.1 12 4.3Z" fill="currentColor" />
        </svg>
      </div>

      <div className="msg-body">
        {turn.phase === "searching" && (
          <div className="progress">
            <span className="spinner" aria-hidden="true" /> Searching the knowledge base…
          </div>
        )}

        {turn.phase === "writing" && !turn.text && (
          <div className="progress">
            <span className="spinner" aria-hidden="true" />
            {sources.length
              ? `Found ${sources.length} relevant passage${sources.length === 1 ? "" : "s"}. Writing the answer…`
              : "Writing the answer…"}
          </div>
        )}

        {status && (status === "refused" || CAUTION.has(status)) && (
          <div className={`status-pill ${status === "refused" ? "danger" : "warn"}`}>
            {STATUS_LABEL[status] ?? status}
          </div>
        )}

        {turn.text && (
          <div className={`answer-text${turn.phase === "writing" ? " streaming" : ""}`}>
            {renderAnswer(turn.text, sources)}
          </div>
        )}

        {turn.phase === "error" && (
          <div className="msg-error" role="alert">
            {turn.error ?? "Something went wrong."}
          </div>
        )}
        {turn.phase === "stopped" && !turn.text && <div className="muted small">Stopped.</div>}
        {turn.phase === "stopped" && turn.text && <div className="muted small">Stopped before the answer finished.</div>}

        {sources.length > 0 && turn.phase !== "searching" && (
          <Sources sources={sources} cited={citedIds(turn.text)} final={turn.phase === "done"} />
        )}

        {result && (
          <div className="msg-meta">
            {result.grounded && <span className="meta-chip ok">Grounded in sources</span>}
            {/* Only say "Database" when a query actually ran: in documents-only
                mode a data-shaped question is still answered from documents. */}
            {result.sql?.generated && result.route !== "document_rag" && (
              <span className="meta-chip">{ROUTE_LABEL[result.route] ?? result.route}</span>
            )}
            <span className="meta-chip subtle">{formatMs(result.timings_ms.total)}</span>
            <button type="button" className="link-button" onClick={() => setDetails((v) => !v)} aria-expanded={details}>
              {details ? "Hide details" : "Details"}
            </button>
          </div>
        )}

        {result && details && (
          <div className="details">
            {result.notes.length > 0 && (
              <ul className="details-notes">
                {result.notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            )}
            {result.sql?.generated && <SqlPane result={result} />}
            {result.rows.length > 0 && <DataPane result={result} />}
            <TracePane result={result} />
          </div>
        )}
      </div>
    </div>
  );
}

/** Evidence labels the answer text actually cites, e.g. [D1] or [D2, 3.1]. */
function citedIds(text: string): Set<string> {
  return new Set(Array.from(text.matchAll(/\[([DS]\d+)/g), (m) => m[1]));
}

function Sources({ sources, cited, final }: { sources: SourceRef[]; cited: Set<string>; final: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const selected = sources.find((s) => s.id === open);

  // Cited sources are the answer's sources; the rest were retrieved but not
  // used. Until the answer is complete, show everything retrieved.
  const primary = final && cited.size ? sources.filter((s) => cited.has(s.id)) : sources;
  const others = sources.filter((s) => !primary.includes(s));
  const visible = showAll ? sources : primary;

  return (
    <div className="sources">
      <div className="sources-label">{final && cited.size ? "Cited sources" : "Retrieved passages"}</div>
      <div className="source-chips">
        {visible.map((s) => (
          <button
            key={s.id}
            type="button"
            className={`source-chip${open === s.id ? " active" : ""}`}
            onClick={() => setOpen((cur) => (cur === s.id ? null : s.id))}
            aria-expanded={open === s.id}
            title={s.section || s.title}
          >
            <span className="source-id">{s.id}</span>
            <span className="source-title">{s.title}</span>
            {s.version && <span className="source-version">v{s.version}</span>}
          </button>
        ))}
        {others.length > 0 && !showAll && (
          <button type="button" className="source-chip more" onClick={() => setShowAll(true)}>
            +{others.length} more retrieved
          </button>
        )}
      </div>
      {selected && (
        <div className="source-preview">
          <div className="source-preview-head">
            <strong>{selected.title}</strong>
            {selected.section && <span className="muted"> · {selected.section.split(">").pop()?.trim()}</span>}
          </div>
          <p className="source-snippet">{selected.snippet}</p>
          <div className="source-preview-meta">
            {selected.effective_date && <span>Effective {selected.effective_date}</span>}
            {selected.authority && <span>{selected.authority}</span>}
            {selected.retrieval_method && <span>via {selected.retrieval_method}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
