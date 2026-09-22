import type { MetaResponse, Persona, Strategy } from "../types";

interface Props {
  meta: MetaResponse | null;
  persona: string;
  strategy: Strategy;
  busy: boolean;
  onPersona: (key: string) => void;
  onStrategy: (value: Strategy) => void;
  onExample: (question: string) => void;
}

const STRATEGY_NOTE: Record<Strategy, string> = {
  dense: "Embeddings only. Strong on paraphrase, weak on exact identifiers.",
  sparse: "BM25 only. Perfect on exact codes, collapses on multilingual queries.",
  hybrid: "Reciprocal-rank fusion of both. +0.069 NDCG over dense on the held-out set.",
  reranked: "Hybrid, then a cross-encoder. Slower, and not statistically better than hybrid.",
};

export default function Sidebar({
  meta,
  persona,
  strategy,
  busy,
  onPersona,
  onStrategy,
  onExample,
}: Props) {
  const active: Persona | undefined = meta?.personas.find((p) => p.key === persona);

  return (
    <aside className="sidebar">
      <div className="field">
        <label className="field__label" htmlFor="persona">
          Acting as
        </label>
        <select
          id="persona"
          className="select"
          value={persona}
          disabled={!meta || busy}
          onChange={(e) => onPersona(e.target.value)}
        >
          {(meta?.personas ?? []).map((p) => (
            <option key={p.key} value={p.key}>
              {p.label}
            </option>
          ))}
        </select>

        {active && (
          <div className="persona-card">
            <div className="persona-card__row">
              <span className="persona-card__key">Tenant</span>
              <span className="persona-card__value">
                {active.tenant} (#{active.tenant_id})
              </span>
            </div>
            <div className="persona-card__row">
              <span className="persona-card__key">Access</span>
            </div>
            <div className="chips">
              {active.access_groups.map((group) => (
                <span key={group} className="badge badge--neutral">
                  {group}
                </span>
              ))}
            </div>
          </div>
        )}

        <p className="field__hint">
          Permissions are enforced during retrieval and in the SQL guard, not by hiding results
          afterwards. Switch persona and ask the same question to see it.
        </p>
      </div>

      <div className="field">
        <label className="field__label" htmlFor="strategy">
          Retrieval strategy
        </label>
        <select
          id="strategy"
          className="select"
          value={strategy}
          disabled={!meta || busy}
          onChange={(e) => onStrategy(e.target.value as Strategy)}
        >
          {(meta?.strategies ?? ["reranked"]).map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <p className="field__hint">{STRATEGY_NOTE[strategy]}</p>
      </div>

      {meta && meta.example_questions.length > 0 && (
        <div className="field">
          <span className="field__label">Try one of these</span>
          <div className="example-list">
            {meta.example_questions.map((question) => (
              <button
                key={question}
                className="example"
                disabled={busy}
                onClick={() => onExample(question)}
              >
                {question}
              </button>
            ))}
          </div>
        </div>
      )}

      {meta && (
        <div className="field">
          <span className="field__label">Corpus</span>
          <div className="persona-card">
            <div className="persona-card__row">
              <span className="persona-card__key">Documents</span>
              <span className="persona-card__value">{meta.document_count}</span>
            </div>
            <div className="persona-card__row">
              <span className="persona-card__key">Chunks</span>
              <span className="persona-card__value">{meta.chunk_count}</span>
            </div>
            <div className="persona-card__row">
              <span className="persona-card__key">Index</span>
              <span className="persona-card__value">{meta.index_version}</span>
            </div>
            <div className="persona-card__row">
              <span className="persona-card__key">SQL via</span>
              <span className="persona-card__value">{meta.sql_provider}</span>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
