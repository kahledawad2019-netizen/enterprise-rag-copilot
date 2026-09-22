# Security

## Threat model

The system executes SQL written by a language model and answers from documents
that may have been tampered with. Both are untrusted input.

| Threat | Defence | Verified |
|---|---|---|
| Model writes a destructive query | router refuses by rule before SQL exists | `test_routing.py` |
| Model writes a write query anyway | sqlglot AST guard | 46 tests |
| Guard is bypassed or buggy | read-only DB principal | **not yet in force** — see below |
| Cross-tenant read | tenant predicate checked on the parse tree | `test_sql_guard.py` |
| Reading the audit trail | `ai` excluded from catalog and allow-list; `DENY SELECT` | `test_sql_guard.py` |
| Injection in a retrieved document | structural prompt boundary + guard + router | `test_generation.py` |
| Injection in the question | router rules | `test_routing.py` |
| Credential leak via logs or traces | redaction on every write | `redaction.py` |
| Permission bypass in retrieval | payload filters applied during search | eval: 0 leaks / 42 |
| Fabricated citation | validated against the evidence after generation | `test_generation.py` |

## Two layers, and only one is currently in force

**Layer 1 — the SQL guard.** Application code. Parses with sqlglot and
inspects the tree.

**Layer 2 — the database principal.** A login that physically cannot write.

Layer 1 can be defeated by a bug or a refactor. Layer 2 cannot be defeated by
anything short of database compromise. **Ship both.**

> ### Current status: layer 2 is missing
>
> The instance is in Windows-Authentication-only mode
> (`IsIntegratedSecurityOnly = 1`), so `CREATE LOGIN ... WITH PASSWORD` fails
> and `copilot_reader` cannot be created. The connected login is also
> `sysadmin`, which bypasses every database-level `DENY`.
>
> **Right now, only the application guard is protecting the data.**
>
> `check_environment.py` reports this on every run and the Streamlit
> configuration page shows it in red. Fixing it requires enabling Mixed Mode
> and restarting the service — a server-level change this project will not make
> silently. See [ADR-003](architecture_decisions/ADR-003-sql-server-readonly-principal.md).

## Why parsing, not pattern matching

Every one of these defeats a keyword blocklist:

```sql
SELECT 1; /*x*/ DROP TABLE core.customers    -- comment between statements
SELECT * FROM core/**/.customers             -- comment inside an identifier
EXECUTE ('DROP TABLE x')                     -- spelling variant
SELECT * FROM [core].[customers] WHERE 1=1 --
```

sqlglot parses to a syntax tree. A comment cannot hide a node, and an
alternative spelling parses to the same node type. Regex runs only as a
second pass, never as the primary check.

The subtlest case tested: `WHERE customer_name = 'tenant_id = 1'`. A regex
looking for a tenant predicate accepts it. The AST does not, because the text
is a string literal, not a comparison.

## Guard checks

| Check | Refuses |
|---|---|
| Statement count | more than one statement |
| Statement type | anything that is not SELECT / CTE |
| Write nodes | INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, MERGE, TRUNCATE, GRANT — anywhere in the tree |
| `SELECT ... INTO` | creates a table; parses as a Select so needs its own check |
| Schema allow-list | anything outside analytics/core/billing/support |
| Unqualified tables | cannot be checked, so not assumed safe |
| Column block-list | password_hash, api_key, secret, token, ssn, tax_id |
| Tenant predicate | missing, or restricted to a different tenant |
| Join cap | more than 8 joins |
| Suspicious functions | OPENROWSET, OPENQUERY, xp_*, sp_executesql, WAITFOR, DBCC |

On success the guard **rewrites** the query to add `TOP (n)` when no limit is
present, working on the parse tree so unusual formatting cannot defeat it.

## Execution

```
validate -> audit(attempt) -> execute with timeout -> cap rows
         -> redact columns -> audit(outcome)
```

The audit row is written **before** execution. A query that hangs, crashes the
process, or is killed still leaves a record — an audit written only on success
misses exactly the events worth investigating.

Column redaction runs on the *result* as well as the query, because
`SELECT *` over a view can return a blocked column without naming it.

## Prompt injection

Retrieved documents are untrusted. The corpus deliberately contains
`DOC-TST-001`, full of instruction-override payloads, clearly marked as test
data and excluded from normal retrieval.

Defence is structural, not a plea:

1. evidence is delimited and labelled as quoted data
2. the rule is stated **before** the evidence appears
3. the rule is repeated **after** it, because models attend most to the start
   and end of a context window and an injection sits in the middle

**Verified:** with the payloads forced into the evidence package, the model
answered the legitimate question from `[D5]` and ignored every embedded
command. No credentials, no SQL, no mode change.

No prompt is a complete defence. This is one layer; the guard and the
(pending) read-only principal are the others, and none relies on the model
behaving well.

## Secrets

- The DB password is a pydantic `SecretStr` — logging, printing or JSON-dumping
  the settings shows `**********`
- The only unwrap is inside the connection-string builder, whose output is
  never logged; `safe_odbc_connection_string()` exists for display
- A blank `MSSQL_PASSWORD=` means "look in the Windows Credential Manager"
- `.env` is gitignored; `check_environment.py` fails if that stops being true
- Traces and logs pass through `redaction.py` before being written
