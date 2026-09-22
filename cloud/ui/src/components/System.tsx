import { useCallback, useEffect, useState } from "react";

import { CopilotApiError, getHealth, getMeta } from "../api";
import type { HealthResponse, MetaResponse } from "../types";

/**
 * What is configured, what is broken, and what to do about it.
 *
 * A health endpoint that returns `{ok: false}` tells an operator that
 * something is wrong and nothing about which of a dozen environment variables
 * to change. This screen maps each failing check to the specific fix, because
 * the gap between "database: false" and "set the Neon POSTGRES_DSN secret"
 * is where deployments stall.
 *
 * `data_locality` is deliberately given its own panel rather than sitting in
 * the list. It is not a failure - it is a disclosure, and burying a
 * disclosure among green ticks is how it stops being one.
 */

/** Check name -> what to do when it fails. */
const REMEDIES: Record<string, { title: string; body: string; fix?: string }> = {
  copilot: {
    title: "The orchestrator did not start",
    body:
      "The container is serving, but the copilot itself failed to construct. Every other check " +
      "below is downstream of this one. The reason is in the container logs.",
  },
  vector_store: {
    title: "No index to retrieve from",
    body:
      "The Qdrant collection is missing or empty, so document retrieval returns nothing. This is " +
      "also what you will see after changing the embedding model without rebuilding.",
    fix: "python scripts/build_index.py --rebuild",
  },
  database: {
    title: "PostgreSQL is not reachable",
    body:
      "Document questions still work; anything needing data does not. Check the host, that the " +
      "Neon project is active, and that the application DSN uses the read-only copilot_app role.",
    fix: "DATABASE_BACKEND=postgresql  POSTGRES_DSN=postgresql://copilot_app:…@…/…?sslmode=require",
  },
  chat_model: {
    title: "No model to generate with",
    body:
      "Routing falls back to rules and no answer can be written. A container has no GPU, so " +
      "Ollama is not an option there - point it at a hosted provider.",
    fix: "LLM_PROVIDER=openai  LLM_BASE_URL=https://api.groq.com/openai/v1  LLM_MODEL=…",
  },
};

export default function System() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);

  const refresh = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const [h, m] = await Promise.all([getHealth(), getMeta().catch(() => null)]);
      setHealth(h);
      setMeta(m);
    } catch (err) {
      setError(err instanceof CopilotApiError ? err.message : "Could not reach the backend.");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const locality = health?.checks.find((c) => c.name === "data_locality");
  const operational = health?.checks.filter((c) => c.name !== "data_locality") ?? [];
  const failing = operational.filter((c) => !c.ok);

  const leaves = locality?.detail.includes("leaves machine: yes");

  return (
    <div className="page">
      <header className="page__head">
        <h1 className="page__title">System</h1>
        <p className="page__sub">
          Every dependency, its live state, and what to change when it is wrong.
        </p>
      </header>

      <div className="probe-bar">
        <button className="btn" disabled={busy} onClick={() => void refresh()}>
          {busy ? "Checking…" : "Re-check"}
        </button>
        {health && (
          <span
            className={`badge ${
              health.status === "ok"
                ? "badge--ok"
                : health.status === "degraded"
                  ? "badge--warn"
                  : "badge--danger"
            }`}
          >
            <span className="dot" />
            {health.status === "ok"
              ? "All dependencies reachable"
              : health.status === "degraded"
                ? `${failing.length} dependency${failing.length === 1 ? "" : " issues"} — partial service`
                : "Backend down"}
          </span>
        )}
      </div>

      {error && (
        <div className="notice notice--danger">
          <span className="notice__icon">✕</span>
          <span>{error}</span>
        </div>
      )}

      {/* Data locality first. It is a disclosure, not a status line. */}
      {locality && (
        <section className={`card ${leaves ? "card--flagged" : ""}`}>
          <header className="card__head">
            <strong style={{ fontSize: 13 }}>Data locality</strong>
            <span className="card__spacer" />
            <span className={`badge ${leaves ? "badge--warn" : "badge--ok"}`}>
              {leaves ? "data leaves this machine" : "fully local"}
            </span>
          </header>
          <div className="pane">
            <div className="mono-line">{locality.detail}</div>
            <p className="field__hint" style={{ marginTop: 10 }}>
              {leaves ? (
                <>
                  Prompts — including retrieved passages and the schema subset — are sent to a
                  third party. <strong>Query results are not</strong>, and no safety property
                  moves: generated SQL still passes the sqlglot guard and the read-only runner.
                  The claim in <code>README.md</code> that nothing leaves this machine does not
                  hold in this configuration.
                </>
              ) : (
                <>
                  Chat and embeddings both run locally. Nothing about a question, the schema or
                  the documents is sent anywhere.
                </>
              )}
            </p>
          </div>
        </section>
      )}

      <section className="card">
        <header className="card__head">
          <strong style={{ fontSize: 13 }}>Dependencies</strong>
        </header>
        <div className="pane">
          {operational.length === 0 && !busy && (
            <div className="empty">The backend returned no checks.</div>
          )}
          {operational.map((check) => (
            <div key={check.name} className="check">
              <span className={`badge ${check.ok ? "badge--ok" : "badge--danger"}`}>
                {check.ok ? "ok" : "fail"}
              </span>
              <div className="check__body">
                <div className="check__name">{check.name}</div>
                <div className="check__detail">{check.detail}</div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {failing.length > 0 && (
        <section className="card">
          <header className="card__head">
            <strong style={{ fontSize: 13 }}>What to fix</strong>
            <span className="card__spacer" />
            <span className="badge badge--warn">{failing.length}</span>
          </header>
          <div className="pane">
            {failing.map((check) => {
              const remedy = REMEDIES[check.name];
              if (!remedy) {
                return (
                  <div key={check.name} className="notice notice--warn">
                    <span className="notice__icon">!</span>
                    <span>
                      <strong>{check.name}</strong> — {check.detail}
                    </span>
                  </div>
                );
              }
              return (
                <div key={check.name} className="remedy">
                  <div className="remedy__title">{remedy.title}</div>
                  <p className="remedy__body">{remedy.body}</p>
                  {remedy.fix && <pre className="sql-block">{remedy.fix}</pre>}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {meta && (
        <section className="card">
          <header className="card__head">
            <strong style={{ fontSize: 13 }}>Configuration</strong>
          </header>
          <div className="pane">
            <dl className="kv">
              <dt>Chat model</dt>
              <dd>{meta.chat_model}</dd>
              <dt>Text-to-SQL</dt>
              <dd>{meta.sql_provider}</dd>
              <dt>Index version</dt>
              <dd>{meta.index_version}</dd>
              <dt>Corpus</dt>
              <dd>
                {meta.document_count} documents · {meta.chunk_count} chunks
              </dd>
              <dt>Default strategy</dt>
              <dd>{meta.default_strategy}</dd>
              <dt>Personas</dt>
              <dd>{meta.personas.map((p) => p.key).join(", ")}</dd>
            </dl>
            <p className="field__hint" style={{ marginTop: 12 }}>
              No credential is shown here, and there is no code path that could put one in this
              payload — the backend reports keys as set or not set and never their value.
            </p>
          </div>
        </section>
      )}
    </div>
  );
}
