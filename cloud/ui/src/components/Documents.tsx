import { useEffect, useMemo, useState } from "react";

import { CopilotApiError, getDocuments } from "../api";
import type { DocumentSummary } from "../types";

/**
 * The corpus, as the current persona is allowed to see it.
 *
 * The permission count is the point of this screen, not a footnote. Switching
 * from admin to guest takes the list from 19 documents to 5, and the 14 that
 * vanish include the prompt-injection test document and the finance-only
 * pricing policy. That is the access model demonstrated rather than asserted.
 *
 * Filtering happens on the server. Sending all 19 and hiding rows here would
 * put the titles of documents a guest cannot read into a payload a guest
 * receives.
 */

const STATUS_TONE: Record<string, string> = {
  current: "badge--ok",
  superseded: "badge--warn",
  draft: "badge--neutral",
  retired: "badge--danger",
};

interface Props {
  persona: string;
  personaLabel: string;
}

export default function Documents({ persona, personaLabel }: Props) {
  const [docs, setDocs] = useState<DocumentSummary[]>([]);
  const [hidden, setHidden] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");
  const [showSuperseded, setShowSuperseded] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    getDocuments(persona)
      .then((response) => {
        if (cancelled) return;
        setDocs(response.documents);
        setHidden(response.hidden_by_permissions);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof CopilotApiError ? err.message : "Could not load the corpus.");
        }
      })
      .finally(() => !cancelled && setLoading(false));

    return () => {
      cancelled = true;
    };
  }, [persona]);

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return docs.filter((doc) => {
      if (!showSuperseded && doc.status === "superseded") return false;
      if (!needle) return true;
      return (
        doc.title.toLowerCase().includes(needle) ||
        doc.doc_id.toLowerCase().includes(needle) ||
        doc.tags.some((tag) => tag.toLowerCase().includes(needle))
      );
    });
  }, [docs, filter, showSuperseded]);

  const supersededCount = docs.filter((d) => d.status === "superseded").length;

  return (
    <div className="page">
      <header className="page__head">
        <h1 className="page__title">Corpus</h1>
        <p className="page__sub">
          What the copilot can retrieve from, for the persona you are acting as. Versions matter
          here: a superseded policy is kept on purpose, so that answering a current question with
          it is a mistake the evaluation can catch.
        </p>
      </header>

      <div className="notice notice--info">
        <span className="notice__icon">i</span>
        <span>
          As <strong>{personaLabel}</strong>: <strong>{docs.length}</strong> document
          {docs.length === 1 ? "" : "s"} readable
          {hidden > 0 ? (
            <>
              , <strong>{hidden}</strong> withheld by the access model. Switch persona in the
              sidebar to see which.
            </>
          ) : (
            <>. This persona can read everything.</>
          )}
        </span>
      </div>

      <div className="probe-bar">
        <input
          className="input"
          value={filter}
          placeholder="Filter by title, id or tag…"
          onChange={(e) => setFilter(e.target.value)}
        />
        <button
          className="btn"
          aria-pressed={showSuperseded}
          onClick={() => setShowSuperseded((v) => !v)}
        >
          {showSuperseded ? "Hide" : "Show"} superseded ({supersededCount})
        </button>
      </div>

      {error && (
        <div className="notice notice--danger">
          <span className="notice__icon">✕</span>
          <span>{error}</span>
        </div>
      )}

      {loading && (
        <div className="thinking">
          <span className="spinner" />
          <span>Loading the corpus…</span>
        </div>
      )}

      {!loading && !error && visible.length === 0 && (
        <div className="empty">Nothing matches that filter.</div>
      )}

      <div className="doc-grid">
        {visible.map((doc) => (
          <article key={doc.doc_id} className="card doc">
            <header className="card__head">
              <span className="source__tag">{doc.doc_id}</span>
              <span className={`badge ${STATUS_TONE[doc.status] ?? "badge--neutral"}`}>
                {doc.status || "unknown"}
              </span>
              <span className="card__spacer" />
              <span className="badge badge--neutral">v{doc.version || "—"}</span>
            </header>

            <div className="pane">
              <div className="doc__title">{doc.title}</div>

              <dl className="kv" style={{ marginTop: 8, paddingTop: 0, borderTop: "none" }}>
                <dt>Effective</dt>
                <dd>{doc.effective_date || "—"}</dd>
                <dt>Authority</dt>
                <dd>{doc.authority || "—"}</dd>
                <dt>Owner</dt>
                <dd>
                  {doc.owner || "—"}
                  {doc.department ? ` · ${doc.department}` : ""}
                </dd>
                <dt>Length</dt>
                <dd>{doc.words.toLocaleString()} words</dd>
              </dl>

              {(doc.supersedes || doc.superseded_by) && (
                <div
                  className={`notice ${doc.superseded_by ? "notice--warn" : "notice--info"}`}
                  style={{ marginTop: 10, marginBottom: 0 }}
                >
                  <span className="notice__icon">{doc.superseded_by ? "!" : "i"}</span>
                  <span>
                    {doc.superseded_by ? (
                      <>
                        Superseded by <code>{doc.superseded_by}</code>. It must never answer a
                        current question.
                      </>
                    ) : (
                      <>
                        Supersedes <code>{doc.supersedes}</code>.
                      </>
                    )}
                  </span>
                </div>
              )}

              {doc.tags.length > 0 && (
                <div className="chips" style={{ marginTop: 10 }}>
                  {doc.tags.map((tag) => (
                    <span key={tag} className="badge badge--neutral">
                      {tag}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
