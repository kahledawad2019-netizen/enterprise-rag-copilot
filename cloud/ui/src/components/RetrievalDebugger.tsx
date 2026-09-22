import { useCallback, useState } from "react";

import { CopilotApiError, retrieve } from "../api";
import { formatMs } from "../lib/render";
import type { MetaResponse, RetrieveResponse, ScoredChunk, Strategy } from "../types";

/**
 * Side-by-side retrieval, so the strategy comparison can be watched rather
 * than taken on trust.
 *
 * The example queries below are the ones that actually separate the
 * strategies. `INC-2025-0042` is the important one: dense returns the wrong
 * incident at rank 1 because two postmortems are semantically near-identical,
 * sparse gets it right in about a millisecond, and hybrid keeps the sparse
 * answer. That single case is the argument for fusion.
 */

const PROBES: Array<{ query: string; why: string }> = [
  {
    query: "INC-2025-0042",
    why: "Dense returns the WRONG incident. Sparse gets it at rank 1 in ~1 ms.",
  },
  {
    query: "What is the refund policy for enterprise annual plans?",
    why: "Two versions exist. Watch whether the superseded one appears.",
  },
  {
    query: "How quickly must we respond to a P1?",
    why: "Paraphrase — no shared keywords with the policy text. Dense territory.",
  },
  {
    query: "maximum discount a rep can approve",
    why: "Policy says 25%, guidance says 30%. An authority conflict.",
  },
];

const STRATEGY_ORDER: Strategy[] = ["dense", "sparse", "hybrid", "reranked"];

interface Props {
  meta: MetaResponse | null;
  persona: string;
}

export default function RetrievalDebugger({ meta, persona }: Props) {
  const [query, setQuery] = useState(PROBES[0].query);
  const [limit, setLimit] = useState(8);
  const [result, setResult] = useState<RetrieveResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (q: string) => {
      const text = q.trim();
      if (!text || busy) return;
      setBusy(true);
      setError(null);
      try {
        setResult(await retrieve({ query: text, persona, limit }));
      } catch (err) {
        setError(err instanceof CopilotApiError ? err.message : "Retrieval failed.");
        setResult(null);
      } finally {
        setBusy(false);
      }
    },
    [busy, persona, limit],
  );

  const runs = result
    ? [...result.runs].sort(
        (a, b) => STRATEGY_ORDER.indexOf(a.strategy) - STRATEGY_ORDER.indexOf(b.strategy),
      )
    : [];

  // Whether the four strategies agree on rank 1 is the single most useful
  // signal on this screen, so it is stated rather than left to be spotted.
  const topDocs = new Set(runs.map((r) => r.results[0]?.doc_id).filter(Boolean));
  const disagree = topDocs.size > 1;

  return (
    <div className="page">
      <header className="page__head">
        <h1 className="page__title">Retrieval debugger</h1>
        <p className="page__sub">
          One query, every strategy, with the scores that produced each ranking. This is how
          retrieval gets tuned rather than guessed at.
        </p>
      </header>

      <div className="probe-bar">
        <input
          className="input"
          value={query}
          placeholder="Query"
          disabled={busy}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run(query)}
        />
        <select
          className="select select--compact"
          value={limit}
          disabled={busy}
          onChange={(e) => setLimit(Number(e.target.value))}
        >
          {[5, 8, 10, 15, 20].map((n) => (
            <option key={n} value={n}>
              top {n}
            </option>
          ))}
        </select>
        <button className="btn btn--primary" disabled={busy || !query.trim()} onClick={() => run(query)}>
          {busy ? "Running…" : "Compare"}
        </button>
      </div>

      <div className="probe-list">
        {PROBES.map((probe) => (
          <button
            key={probe.query}
            className="probe"
            disabled={busy}
            onClick={() => {
              setQuery(probe.query);
              run(probe.query);
            }}
          >
            <span className="probe__query">{probe.query}</span>
            <span className="probe__why">{probe.why}</span>
          </button>
        ))}
      </div>

      {meta && (
        <p className="field__hint" style={{ marginTop: -6 }}>
          Retrieving as <strong>{meta.personas.find((p) => p.key === persona)?.label ?? persona}</strong>.
          Permission filters apply here too — switch persona in the sidebar and the results change.
        </p>
      )}

      <p className="field__hint" style={{ marginTop: -4 }}>
        <strong>Scores will not always descend with rank, and that is correct.</strong> After
        scoring, two stages deliberately reorder: authority preference puts binding policy above
        advisory guidance, and MMR (λ=0.7) trades a little relevance for diversity, so a slightly
        lower-scoring passage from a different section can outrank a near-duplicate of the one
        above it.
      </p>

      {error && (
        <div className="notice notice--danger">
          <span className="notice__icon">✕</span>
          <span>{error}</span>
        </div>
      )}

      {busy && (
        <div className="thinking">
          <span className="spinner" />
          <span>Running every strategy…</span>
        </div>
      )}

      {result && !busy && (
        <>
          <div className={`notice ${disagree ? "notice--warn" : "notice--info"}`}>
            <span className="notice__icon">{disagree ? "!" : "i"}</span>
            <span>
              {disagree ? (
                <>
                  <strong>The strategies disagree on rank 1.</strong> {topDocs.size} different
                  documents are being returned first. This is the case fusion exists for — compare
                  the columns below.
                </>
              ) : (
                <>All strategies agree on the top result. This query does not separate them.</>
              )}
            </span>
          </div>

          <div className="compare-grid">
            {runs.map((run) => (
              <section key={run.strategy} className="card compare-col">
                <header className="card__head">
                  <span className="badge badge--accent">{run.strategy}</span>
                  <span className="card__spacer" />
                  <span className="badge badge--neutral">{formatMs(run.elapsed_ms)}</span>
                </header>
                <div className="pane">
                  {run.error ? (
                    <div className="notice notice--danger">
                      <span className="notice__icon">✕</span>
                      <span>{run.error}</span>
                    </div>
                  ) : run.results.length === 0 ? (
                    <div className="empty">No results.</div>
                  ) : (
                    run.results.map((item) => <Hit key={`${item.rank}-${item.doc_id}`} item={item} />)
                  )}
                </div>
              </section>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function Hit({ item }: { item: ScoredChunk }) {
  return (
    <div className={`hit${item.rank === 1 ? " hit--top" : ""}`}>
      <span className="hit__rank">{item.rank}</span>
      <div className="hit__body">
        <div className="hit__doc">{item.doc_id}</div>
        <div className="hit__meta">
          {item.version && <span>v{item.version}</span>}
          {item.section && <span title={item.section}>{truncate(item.section, 42)}</span>}
        </div>
        <div className="hit__scores">
          <Score label="score" value={item.score} />
          {item.dense_score !== null && (
            <Score label="dense" value={item.dense_score} rank={item.dense_rank} />
          )}
          {item.sparse_score !== null && (
            <Score label="sparse" value={item.sparse_score} rank={item.sparse_rank} />
          )}
          {item.rerank_score !== null && <Score label="rerank" value={item.rerank_score} />}
        </div>
      </div>
    </div>
  );
}

function Score({ label, value, rank }: { label: string; value: number; rank?: number | null }) {
  return (
    <span className="score" title={rank != null ? `${label} rank #${rank}` : label}>
      <span className="score__label">{label}</span>
      <span className="score__value">{value.toFixed(3)}</span>
      {rank != null && <span className="score__rank">#{rank}</span>}
    </span>
  );
}

function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}
