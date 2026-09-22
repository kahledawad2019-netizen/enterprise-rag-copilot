# Observability

## Principles

**The system must run when the collector is down.** A request that fails
because a trace could not be exported has turned a debugging aid into an
outage. Every export path is wrapped; an unreachable collector produces one
warning and the request continues.

**Everything is redacted on the way out.** Traces carry prompts, SQL and result
previews. A trace file outlives the request and is read by people who were
never granted access to the underlying records.

## What is recorded

One trace id per request, one span per stage:

| Span | Attributes |
|---|---|
| `routing` | route, decided_by, confidence, identifiers, prompt version |
| `retrieval` | strategy, dense/sparse counts, chunk ids, scores |
| `sql_generation` | provider, generated SQL, tables offered |
| `sql_execution` | SQL, rows, duration |
| `generation` | evidence count, conflicts, status, grounded, citations, model |

Plus, per request: question, user, tenant, answer status, citation count,
SQL validation verdict, model name, prompt version, index version, errors.

## Redaction

| Category | Treatment | Why |
|---|---|---|
| Connection strings, passwords, keys, bearer tokens | removed entirely | a partial password is still a hint |
| Emails | `j***@example.com` | recognisable for debugging, not usable |
| Phone numbers | `[PHONE]` | |
| Prompts and result sets | truncated | a trace holding the whole evidence package is a copy of the data |
| Lists | capped at 50 items | |

Driver errors routinely echo the connection string back, so `safe_exception()`
redacts exception text too.

## Outputs

| Output | Location | Always on? |
|---|---|---|
| JSONL traces | `data/traces/traces_YYYYMMDD.jsonl` | yes |
| Structured logs | `data/logs/copilot_YYYYMMDD.jsonl` | yes |
| Console logs | stdout | yes |
| OpenTelemetry / Phoenix | OTLP endpoint | only if `OBS_ENABLE_PHOENIX=true` |
| Trace viewer | Streamlit **Traces** page | yes |

## Phoenix (optional)

```powershell
.venv\Scripts\python -m pip install -e ".[phoenix]"
.venv\Scripts\python -m phoenix.server.main serve
```

Then set `OBS_ENABLE_PHOENIX=true`. If it is unreachable, export disables
itself after one warning and local tracing continues.

## Reading a trace

Streamlit **Traces** page, or:

```powershell
Get-Content data\traces\traces_*.jsonl -Tail 1 | ConvertFrom-Json | Format-List
```

Typical breakdown (standard profile):

| stage | typical |
|---|---|
| routing (LLM) | ~4.5 s |
| dense retrieval | ~120 ms |
| sparse retrieval | ~7 ms |
| reranking (CPU) | ~2.0 s |
| SQL generation | ~15-25 s |
| SQL execution | ~35 ms |
| answer generation | ~5-12 s |

**Generation dominates. Retrieval is not the bottleneck** — worth knowing
before optimising the wrong thing.
