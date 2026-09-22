# Troubleshooting

Start here: `.venv\Scripts\python scripts\check_environment.py`

---

## "Storage folder data/qdrant is already accessed by another instance"

**Cause:** embedded Qdrant is single-process, and something else has the index
open — usually the Streamlit app, a notebook kernel, or a previous run.

**Fix:** close the other process. To run several at once, switch to
`QDRANT_MODE=server` and start Qdrant via `docker/docker-compose.yml`.

---

## "Cannot connect to SQL Server"

```powershell
Get-Service MSSQL*                      # is the engine installed and running?
Start-Service 'MSSQL$SQLEXPRESS'
```

- Nothing listed: the database engine is not installed. SSMS is a client, not a
  server. See `docs/setup_windows.md`.
- Named instance: `MSSQL_SERVER=.\SQLEXPRESS` (escaped as `.\\SQLEXPRESS` in
  some shells).
- TCP disabled: enable it in SQL Server Configuration Manager, then restart.

---

## "Invalid column name 'tenant_id'"

**Cause:** the query filters on `tenant_id` for an object that has no such
column — reference tables like `support.sla_policies`, or `vw_month_spine`.

**Fix:** already handled by `SECURITY_TENANT_EXEMPT_OBJECTS`. If you add a new
reference table, add it to that list in `config/settings.py`.

---

## "Embedding failed" / "model not found"

```powershell
ollama list
ollama pull qwen3-embedding:0.6b
ollama pull llama3.1:8b
```

Ollama not running: start the app, or `ollama serve`.

---

## "Collection was built with dimension N but the model produces M"

**Cause:** the embedding model changed after the index was built.

**Fix:** `python scripts\build_index.py --rebuild`

This error is deliberately loud. If two models happened to share a dimension,
the index would return confident nonsense instead of failing — which is far
worse.

---

## Answers are ungrounded / no citations

1. Is the index built? `python scripts\build_index.py --validate`
2. Is retrieval finding anything? `python scripts\search.py "your question"`
3. Is the user permitted? A `guest` legitimately cannot reach finance documents.
4. Is the document superseded? Retrieval excludes those by default.

---

## Everything is slow

| Symptom | Cause | Fix |
|---|---|---|
| ~2 s added per query | CPU reranking | `RETRIEVAL_ENABLE_RERANKING=false`, or use `hybrid` |
| 15-25 s SQL generation | 8B model on a long prompt | `COPILOT_PROFILE=lite` |
| 6 s once, then fast | reranker model loading | expected; it is cached |
| First run downloads GB | model downloads | expected, once |

Generation dominates. Retrieval is 4-120 ms.

---

## Reranker unavailable

Expected without the `rerank` extra. The system degrades to the fused ranking
and *says so* — `reranker.describe()` reports `active: false` with a reason.
Install with `pip install -e ".[rerank]"` (~2.5 GB).

---

## Vanna errors

| Symptom | Cause |
|---|---|
| `ImportError` | not installed — `pip install -e ".[vanna]"`; the native provider is used automatically |
| SQL truncated at `[` | Vanna's own `extract_sql`; ours overrides it — check you are not calling Vanna directly |
| `vanna.__version__` says 0.1.0 | it is wrong; the distribution is 2.0.2. Use `importlib.metadata` |

---

## Streamlit: "multiple elements with the same auto-generated ID"

**Cause:** a widget without an explicit `key`, or a page module that renders on
import.

**Fix:** shared helpers live in `app/_shared.py` precisely so importing them has
no side effects. Every widget passes `key=`.

---

## Tests fail on a fresh machine

Integration tests skip with a clear reason when SQL Server, Ollama or the index
is unavailable. A *skip* is not a pass — read the reason.

```powershell
.venv\Scripts\python -m pytest -q -m "not integration"   # unit tests only
```

---

## Evaluation numbers moved

Expected after changing chunk size, the embedding model, `k`, or the reranker.

```powershell
.venv\Scripts\python scripts\evaluate_retrieval.py --k 8 --compare-baseline
```

Non-zero exit means a regression beyond 0.02 NDCG. If the change was intended,
re-save the baseline with `--save-baseline`.

**Never tune against the held-out set.** If you do, it stops being held out and
a new one must be generated.
