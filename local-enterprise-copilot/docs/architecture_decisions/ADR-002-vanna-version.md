# ADR-002: Vanna version, the `legacy` API, and isolation behind our own interface

- **Status:** Accepted
- **Date:** 2026-09-18
- **Deciders:** Lead engineer

## Context

Vanna AI is required as the default Text-to-SQL integration. The package went
through a breaking redesign between the 0.x line (which nearly every tutorial
online targets) and 2.x, so "install vanna and follow a blog post" produces
code that does not run.

### What is actually installed

Resolving `vanna[chromadb,ollama]` today yields **vanna 2.0.2**, not the 0.7.x
that most examples assume. Inspecting the installed package rather than trusting
documentation showed two parallel APIs shipping in the same distribution:

```
vanna/
├── agents/        2.x agent framework
├── core/          2.x interfaces (Agent, LlmService, Tool, ToolRegistry, ...)
├── integrations/  2.x connectors (mssql, ollama, chromadb, qdrant, ...)
└── legacy/        the classic 0.x API, still present and functional
    ├── base/      VannaBase, with connect_to_mssql(), train(), ask(), ...
    ├── chromadb/  ChromaDB_VectorStore
    └── ollama/    Ollama
```

Confusingly, `vanna.__version__` reports `"0.1.0"` while the distribution
version is `2.0.2`. Never infer the version from `vanna.__version__`; use
`importlib.metadata.version("vanna")`.

Both APIs were exercised against the live SQL Server instance. The classic
interface (`vanna.legacy.chromadb.ChromaDB_VectorStore` +
`vanna.legacy.ollama.Ollama`, with `VannaBase.connect_to_mssql`) works
end to end: it trained on a real schema and generated correct T-SQL with a
proper join against a local llama3.1.

### A real defect found in the installed version

`vanna.legacy.ollama.Ollama.extract_sql` cuts the model's response at the first
`[` character:

```python
select_with = re.search(r"(select|(with.*?as \())(.*?)(?=;|\[|```)", ...)
```

The `\[` in the lookahead is a workaround aimed at Mistral. On SQL Server it is
actively harmful, because bracket-quoted identifiers are ordinary T-SQL. A model
that emits

```sql
SELECT [State], COUNT([CustomerID]) AS [CustomerCount] FROM [Customers] GROUP BY [State]
```

has its query silently truncated to `SELECT `, which then fails at the server
with a syntax error that points nowhere near the cause. This was observed
directly, not inferred.

That single defect is the clearest possible statement of the maintenance risk:
the library's SQL-extraction layer is not safe for the dialect we target, and
we cannot rely on an upstream fix arriving.

## Decision

**1. Pin `vanna==2.0.2` exactly** in the `vanna` optional extra. No range. A
floating pin on a library with a rewrite in its recent history is an outage
waiting for a `pip install -U`.

**2. Use the `vanna.legacy.*` API, not the 2.x agent framework.** The legacy
surface is small, synchronous, readable, and does exactly one job we want
(retrieve context → prompt → SQL). The 2.x agent framework would take ownership
of routing, tool execution and conversation state — all of which this project
implements itself, deliberately and testably, per the requirement to avoid
coupling to a single orchestration framework.

**3. Treat Vanna as a replaceable component behind our own interface.**

```python
class TextToSQLProvider(Protocol):
    def generate_query(self, request: SQLRequest) -> GeneratedSQL: ...
    def validate_query(self, sql: str) -> ValidationResult: ...
    def execute_query(self, sql: str, ctx: UserContext) -> QueryResult: ...
    def explain_result(self, result: QueryResult) -> str: ...
    def get_trace_metadata(self) -> dict[str, Any]: ...
```

Two implementations:

- `VannaTextToSQLProvider` — the default, wrapping `vanna.legacy`.
- `NativeTextToSQLProvider` — a direct implementation over the same schema
  retriever, business glossary, approved-example store, SQL validator and
  database runner.

Both share every safety-critical component. Vanna supplies *generation only*;
it never validates or executes. Validation is ours (`security/sql_guard.py`,
sqlglot-based) and execution is ours (`database/read_only_runner.py`).

**4. Override `extract_sql`.** The bracket bug is patched in our subclass, with
the reason documented inline, and covered by a regression test so an upstream
change cannot reintroduce it unnoticed.

## Consequences

**Positive**
- The spec's requirement to feature Vanna is met, with its real behaviour
  understood rather than assumed.
- If Vanna breaks, is abandoned, or is dropped, `NativeTextToSQLProvider`
  keeps the product working. Switching is one config value.
- No safety property depends on Vanna behaving correctly.

**Negative**
- Two Text-to-SQL paths to maintain and to keep at parity in evaluation. The
  Text-to-SQL eval suite runs against both, which turns this cost into a
  benefit: it becomes a genuine A/B comparison.
- Pinning exactly means security patches need a deliberate bump.
- Vanna pulls ChromaDB as its own training store, so the project carries two
  vector stores: Qdrant for document RAG, Chroma inside Vanna for SQL examples.
  Accepted because they serve different corpora; `NativeTextToSQLProvider`
  uses Qdrant for both and is the path to consolidating later.

## Maintenance risk note

The upstream repository is reported archived. Combined with what was observed
directly — a rewritten API, a misleading `__version__`, and an unfixed
dialect-breaking bug in the SQL extractor — the prudent posture is to treat
Vanna as **useful but not load-bearing**. This project does exactly that: it
ships Vanna as the default provider because the specification calls for it,
while ensuring no correctness or security guarantee rests on it.

## Verification

- `tests/test_text_to_sql_provider.py` — both providers satisfy the interface.
- `tests/test_vanna_extract_sql.py` — bracketed identifiers, fenced blocks and
  CTEs survive extraction.
- `evals/text_to_sql.jsonl` — scored for both providers.
