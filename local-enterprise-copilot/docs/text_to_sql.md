# Text-to-SQL

## The interface

Vanna is the specified default, but ADR-002 records why nothing safety-critical
depends on it. So the application owns the interface and a provider is
responsible for **generation only**:

```python
class TextToSQLProvider(ABC):
    def generate_query(self, request) -> GeneratedSQL:  ...   # provider
    def explain_result(self, question, result) -> str:  ...   # provider
    def validate_query(self, sql, *, tenant_id):        ...   # OURS, always
    def execute_query(self, sql, *, request):           ...   # OURS, always
    def get_trace_metadata(self) -> dict:               ...
```

`validate_query` and `execute_query` are implemented once on the base class. A
provider that could validate its own SQL could approve its own SQL, so
`tests/test_text_to_sql.py` asserts neither subclass overrides them.

| Provider | Role |
|---|---|
| `VannaTextToSQLProvider` | default, wraps `vanna.legacy` |
| `NativeTextToSQLProvider` | fallback and reference implementation |

Both use the same schema retriever, glossary, examples, guard and runner — so
comparing them measures generation and nothing else.

## Schema retrieval: a subset, not the whole schema

26 objects and ~200 columns rendered as DDL is several thousand tokens on
*every* question. It costs latency, crowds out the evidence, and measurably
degrades accuracy: a model shown fifty tables picks the wrong one far more
often than one shown five.

Selection is hybrid, for the same reason document retrieval is:

- **lexical** catches a question naming a column ("how many SLA breaches")
- **embeddings** catch one that does not ("who are our biggest customers?")
- **analytics views get a deliberate boost**, because steering towards the
  curated surface is the most effective way to stop the model inventing its own
  metric definitions

Related tables are pulled in via foreign keys, so a join the model needs is
never missing from the schema it was shown.

## The glossary is the part most systems omit

The schema says:

```sql
core.subscriptions.mrr_amount   DECIMAL(19,4)
```

A model given only that will write `SUM(mrr_amount)`. That is wrong, and
nothing in the schema says so:

- trials must be excluded
- annual contracts are already normalised to a monthly figure
- the definition **changed** on 2025-01-01; the old one included trials

`ai.business_glossary` carries the definition, the SQL guidance and the explicit
exclusions, and only `is_current = 1` rows are retrieved. Feeding back the
superseded MRR definition would reintroduce the exact error it was retired for.

## Prompt contents

| Section | Source |
|---|---|
| SCHEMA | selected tables as DDL |
| RELATIONSHIPS | foreign keys among those tables |
| BUSINESS DEFINITIONS | `ai.business_glossary`, current only |
| APPROVED EXAMPLES | `ai.approved_sql_examples`, 3 most similar |
| TENANT RESTRICTION | the tenant id, plus the objects that have **no** `tenant_id` column |

That last exclusion list exists because of a real bug: the prompt told the model
to filter every query on `tenant_id`, so it added the predicate to
`support.sla_policies`, which has no such column, and the query failed with
*Invalid column name*.

## Validation

Always ours, never the provider's. See `docs/security.md`. Summary:

parse with sqlglot → single statement → SELECT only → schema allow-list →
column block-list → tenant predicate on the parse tree → join cap → row cap →
audit → execute with timeout → redact → audit again.

## Vanna specifics

Read ADR-002 before touching `vanna_provider.py`.

| Observation | Consequence |
|---|---|
| Distribution is **2.0.2**, not the 0.7.x tutorials assume | pinned exactly |
| `vanna.__version__` reports `"0.1.0"` | never read it; use `importlib.metadata` |
| The classic API survives at `vanna.legacy.*` | used in preference to the 2.x agent framework |
| `Ollama.extract_sql` cuts at the first `[` | **overridden**, with a regression test |

That last one is not theoretical. `SELECT [State], COUNT(*) ...` arrives at the
server as `SELECT `, failing with a syntax error that points nowhere near the
cause. Bracket-quoted identifiers are ordinary T-SQL.

Vanna also owns its own prompt, so the tenant restriction can only be appended
to the question as advice. **Only the guard actually enforces it** — which is
precisely why the guard, not the provider, is the security boundary.
