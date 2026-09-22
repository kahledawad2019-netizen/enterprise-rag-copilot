# Project progress

Updated at the end of every phase, per the implementation plan.
Schema version 1.0.0 · Index version v1 · Seed 20240601

---

## Phase 0 — Environment and architecture ✅ COMPLETE

### Commands run

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
ollama pull qwen3-embedding:0.6b
ollama pull llama3.1:8b
.venv\Scripts\python scripts\check_environment.py
```

### Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Environment inspected | ✅ | 28 checks, 0 failures |
| Blockers identified | ✅ | ADR-001, ADR-003 |
| Architecture + ADRs written | ✅ | 4 ADRs in `docs/architecture_decisions/` |
| Repository structure created | ✅ | Matches spec §22 |
| Model profiles defined | ✅ | lite / standard / high, env-overridable |
| Health-check script | ✅ | `scripts/check_environment.py` |
| Embedding dimension detected, not assumed | ✅ | 1024, probed at runtime |

### Environment findings

| Property | Value |
|---|---|
| OS | Windows 11 Home, build 26200 |
| CPU | AMD Ryzen AI 9 HX 370 (12C/24T) |
| RAM | 31.1 GB |
| GPU | NVIDIA RTX 4070 Laptop, **8 GB VRAM**, CC 8.9 |
| Disk free | C: 51 GB · D: 28 GB |
| Docker | **not installed** → Qdrant embedded mode |
| Python | 3.14.0 (default), 3.13.14 → **venv uses 3.13** |
| Ollama | 0.34.2 · llama3.1:8b, qwen3-embedding:0.6b |
| SQL Server | 2025 **Express** 17.0.1000.7, `.\SQLEXPRESS` |
| SQL auth mode | **Windows Authentication only** |
| Connected login | `Ambitious\ambit` — **sysadmin** |
| ODBC | Driver 17 and 18 installed |

### Blockers and how each was handled

1. **Python 3.11 not available; 3.14 is the default but lacks wheels.**
   Resolved: venv on 3.13, `requires-python = ">=3.11,<3.14"`. Verified by
   dry-run resolving the full stack including torch 2.14 and Phoenix 20.14.
2. **Docker absent.** Resolved: Qdrant embedded mode by default; `docker/`
   compose file provided for teams that do have it. ADR-001.
3. **SQL Server is Windows-auth-only and the login is sysadmin.**
   *Not resolved — deliberately left to the machine owner.* A read-only SQL
   login cannot be created without enabling Mixed Mode and restarting the
   service, which is a server-level security change this project will not make
   silently. `006_create_security.sql` creates the `copilot_readonly` role and
   detects the mode; `check_environment.py` warns on every run. **ADR-003.**

---

## Phase 1 — SQL Server and data ✅ COMPLETE

### Commands run

```powershell
.venv\Scripts\python scripts\setup_database.py           # 001-007
.venv\Scripts\python scripts\generate_synthetic_data.py  # generate + load
.venv\Scripts\python scripts\validate_data.py            # 008
.venv\Scripts\python -m pytest -q
```

### Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Version-controlled SQL scripts 001–008 | ✅ | All applied cleanly, idempotent |
| 6 schemas | ✅ | core, billing, support, analytics, security, ai |
| Normalised tables for every required entity | ✅ | 25 tables, 42 FKs, 27 CHECK constraints |
| 6 analytics views | ✅ | All queryable and non-empty |
| Read-only principal | ⚠️ | Role created (38 perms); login blocked by ADR-003 |
| Reproducible synthetic data | ✅ | `test_generation_is_deterministic` passes |
| Business glossary, ≥19 terms | ✅ | 19 current + 1 superseded (MRR v1.0) |
| Approved SQL examples | ✅ | 10, human-verified |
| Validation queries | ✅ | **43 checks, 0 failed** |
| Tests | ✅ | **41 passed** |

### Data loaded

| Table | Rows |
|---|---|
| core.customers | 600 |
| core.customer_contacts | 1,189 |
| core.subscriptions | 907 |
| core.subscription_changes | 1,386 |
| core.usage_daily | 236,992 |
| billing.invoices | 8,689 |
| billing.invoice_items | 8,689 |
| billing.payments | 8,011 |
| billing.refunds | 279 |
| support.tickets | 4,000 |
| support.ticket_events | 11,210 |
| support.sla_breaches | 712 |
| support.incidents | 9 |
| support.incident_impact | 490 |
| analytics.customer_health | 4,767 |
| **Total** | **~287,000** (loaded in 7.9 s) |

Verified figures: total active MRR **$531,758**, active customers **79.5 %**,
late payments **18.2 %**, SLA breach rate **17.4 %**, at-risk customers **127**,
customers with >3 breaches **31**, June-2025 SEV1 affected **99** customers.

### Defects found and fixed during this phase

1. **Blank `MSSQL_PASSWORD=` became `SecretStr('')`, not `None`.** The
   documented "leave it blank and use the Credential Manager" flow was
   therefore broken: the keyring fallback never fired and an empty password
   would have been sent to the driver. Found by `test_windows_auth_stores_no_password`,
   fixed in `config/settings.py`.

### Unresolved issues carried forward

| Issue | Impact | Owner |
|---|---|---|
| Mixed Mode disabled → no read-only SQL login | Defence in depth incomplete; only the application guard protects data | Machine owner (ADR-003) |
| `TrustServerCertificate=yes` | Fine for local self-signed cert; must be `false` for any remote server | Deployment |
| SQL Server **Express** | 10 GB database cap, ~1.4 GB buffer pool. Current dataset is far below the cap | Accepted |

---

## Phase 2 — Documents and indexing ✅ COMPLETE

### Commands run

```powershell
.venv\Scripts\python scripts\build_index.py --rebuild
.venv\Scripts\python scripts\build_index.py            # incremental
.venv\Scripts\python -m pytest tests\test_ingestion.py -q
```

### Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Synthetic corpus, 14+ document types | ✅ | 19 documents, 7,160 words |
| Multiple versions for freshness testing | ✅ | Refund v1.0/v2.1, SLA v1.0/v2.0 |
| Controlled contradictions | ✅ | 3 kinds: version, version, authority |
| Prompt-injection test document, clearly marked | ✅ | DOC-TST-001, 6 payloads, access_group=security |
| Full metadata on every document | ✅ | YAML front matter, validated by pydantic |
| Markdown / PDF / DOCX parsing | ✅ | 3 parsers behind one registry |
| Header/footer cleanup, content hashing | ✅ | `strip_headers_footers`, SHA-256 |
| Structure-aware chunking | ✅ | 130 chunks; clauses and tables intact |
| Parent-child retrieval | ✅ | Parent section chunk emitted on split |
| Incremental indexing | ✅ | 19/19 skipped on re-run, 2.8 s vs 12.1 s |
| Stale chunk deletion | ✅ | `delete_document` on change or removal |
| Index manifest and validation | ✅ | `IndexManifest.is_compatible_with` |

### Corpus contradictions (deliberate)

| Conflict | Documents | Type |
|---|---|---|
| Annual refunds: "non-refundable" vs "pro-rata within 30 days" | DOC-REF-000 v1.0 → DOC-REF-001 v2.1 | version |
| Enterprise P1 first response: 30 min vs 15 min | DOC-SLA-000 v1.0 → DOC-SLA-001 v2.0 | version |
| Max discount: 25% (policy) vs 30% (guidance) | DOC-PRC-001 vs DOC-SAL-001 | authority |

The SLA conflict mirrors `support.sla_policies` in the database exactly, so the
document corpus and the structured data agree on the same history.

---

## Phase 3 — Retrieval ✅ COMPLETE

### Commands run

```powershell
.venv\Scripts\python scripts\search.py --compare "INC-2025-0042"
.venv\Scripts\python scripts\evaluate_retrieval.py --k 8
.venv\Scripts\python -m pytest tests\test_retrieval.py -q
```

### Measured results (42 evaluation cases, k=8)

| strategy | hit@k | MRR | recall | precision | **NDCG** | filter | mean ms |
|---|---|---|---|---|---|---|---|
| dense | 0.968 | 0.775 | 0.935 | 0.279 | 0.800 | 1.000 | 119 |
| sparse | 0.903 | 0.786 | 0.887 | 0.297 | 0.797 | 1.000 | **4** |
| hybrid | 1.000 | 0.863 | 0.968 | **0.299** | 0.871 | 1.000 | 46 |
| **reranked** | 1.000 | **0.882** | **0.984** | 0.261 | **0.886** | 1.000 | 2152 |

**Hybrid is +8.8 % NDCG over the dense baseline; reranking adds a further
+1.9 %, for +10.7 % overall — at 47x the latency.**

> Measured before reranking became adaptive. The cross-encoder is now
> skipped when dense and sparse independently agree on the top result, so
> the live ratio is roughly 20x rather than 47x. The NDCG figures in this
> table are the historical run and have not been recomputed. See
> [evaluation_report.md](evaluation_report.md) section 7.2.

**Hybrid improves NDCG by +8.8 % over dense; reranking adds +1.9 % more.**
Not asserted - measured, and reproducible with `scripts/evaluate_retrieval.py`.

### Where each method wins (NDCG by category)

| category | dense | sparse | hybrid | reranked |
|---|---|---|---|---|
| exact_code | 0.804 | **1.000** | **1.000** | 0.852 |
| multilingual | **0.877** | 0.333 | **0.877** | **0.877** |
| prompt_injection | 0.631 | **1.000** | **1.000** | **1.000** |
| simple_fact | 0.750 | **1.000** | 0.938 | 0.938 |
| paraphrase | 0.783 | 0.543 | 0.733 | **0.783** |
| version_sensitive | 0.877 | 0.810 | 0.833 | **1.000** |
| date_sensitive | 0.769 | 0.875 | 0.796 | **0.959** |
| ambiguous | 0.500 | 0.000 | 0.500 | **0.631** |
| conflict | **1.000** | **1.000** | **1.000** | 0.850 |

This table is the justification for hybrid retrieval. Sparse is perfect on
exact identifiers and collapses to 0.333 on multilingual queries; dense is the
reverse. Hybrid keeps the better of the two in almost every category.

**Honest caveat:** hybrid is *not* uniformly better. On `paraphrase` dense
scores 0.783 against hybrid's 0.733, and on `simple_fact` sparse scores 1.000
against hybrid's 0.938. Fusion trades a little peak accuracy for far better
worst-case behaviour.

### Concrete failure case that motivates hybrid

Query `INC-2025-0042`:

- **dense** returns `DOC-PM-2025-0031` at rank 1 — *the wrong incident*
- **sparse** returns `DOC-PM-2025-0042` at rank 1, in 1 ms
- **hybrid** returns the correct document at rank 1

### Security and permission results

| Check | Result |
|---|---|
| Filter correctness (no permission or version leak) | **1.000** across all strategies |
| Abstention on unanswerable questions | **7/7** |
| Guest reaching the finance-only pricing policy | never (asserted in tests) |
| Superseded policy leaking into current answers | never |
| Injection document returned by default retrieval | never |

### Unbiased (held-out) evaluation

The 42-case dev set was written by the author, so it proves nothing about
generalisation. A held-out set was generated instead: 96 questions written by
llama3.1:8b from uniformly sampled passages, with mechanical ground truth and
rule-based filtering only. See `docs/evaluation.md`.

| improvement over dense | dev set (authored) | held-out (generated) |
|---|---|---|
| hybrid | +8.8 % | **+8.0 %** |
| reranked | +10.7 % | **+10.7 %** |

The gains reproduce on questions the author never wrote.

**Strongest evidence the gain is real, not an artifact** - NDCG by lexical
overlap between question and source passage:

| overlap | n | dense | sparse | hybrid | reranked |
|---|---|---|---|---|---|
| low (hardest) | 33 | 0.721 | 0.778 | 0.845 | **0.874** |
| medium | 45 | 0.927 | 0.918 | 0.981 | 0.981 |
| high (leakiest) | 18 | 0.927 | 0.959 | 0.931 | **1.000** |

Hybrid beats dense by +17.2 % on the hardest (low-overlap) cases and only
+0.4 % on the easiest. An evaluation artifact would show the opposite pattern.
Sparse swings +23 % from low to high overlap, confirming that lexical leakage
flatters BM25 - which is why the breakdown is reported rather than the mean.

### Defects found and fixed during these phases

1. **Incremental indexing never worked.** `document_hashes()` compared the
   *chunk's* text hash against the *document's* content hash, so nothing was
   ever detected as unchanged and every re-index re-embedded the whole corpus.
   Fixed by carrying `doc_content_hash` on each chunk. Re-index went from
   12.1 s to 2.8 s with 19/19 documents correctly skipped.
2. **`doc_id` validator rejected postmortem ids.** The pattern required
   `DOC-XX-NNN`, but `support.incidents.postmortem_doc_id` already held
   `DOC-PM-2025-0042`. The document corpus and the database would not have
   linked up. Pattern widened.

### Unresolved issues carried forward

| Issue | Impact |
|---|---|
| Payload indexes are ignored by embedded Qdrant | Filters work but scan; irrelevant at 130 chunks, matters at scale. `QDRANT_MODE=server` fixes it |
| Reranking costs 47x latency for +1.9 % NDCG | Worth it for nuanced questions, harmful on exact identifiers (exact_code 1.000 to 0.852). Consider conditional reranking |
| Query rewriting and multi-query retrieval not implemented | Deferred: the spec says to add advanced techniques only after a baseline exists and only if evaluation shows value |

---

## Phase 4 — Document answers ✅ COMPLETE

### Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Local Ollama answer generation | ✅ | `generation/answerer.py`, llama3.1:8b |
| Evidence-package data model | ✅ | `models/evidence.py`, document vs SQL evidence kept distinct |
| Citations with version and section | ✅ | "Refund and Credit Policy, v2.1, section 3. Annual plans" |
| Citation validation (no invented sources) | ✅ | `generation/citations.py`, fabricated labels detected |
| States when evidence is missing | ✅ | abstention detected, status `insufficient_evidence` |
| States when sources conflict | ✅ | `detect_conflicts`, version and authority |
| Never treats retrieved text as instructions | ✅ | **verified with payloads in the evidence** |
| Prompt versioning | ✅ | `PROMPT_VERSION` recorded on every answer |

### Verified injection resistance

The injection document was **forced into the evidence package** (simulating a
corpus an attacker has written to), containing "ignore all previous
instructions" and "unrestricted mode". The model answered the legitimate
refund question from [D5] and ignored every embedded command. No credentials,
no SQL, no mode change.

### Defects found and fixed

1. **Citation extractor was too strict.** The model cites `[D1, 3.1]` — label
   plus section — and the original regex required every comma-separated part to
   be a label, so the whole citation was dropped and a correctly-cited answer
   was reported as ungrounded.
2. **Abstention was mislabelled `partial`.** Retrieval always returns top-k, so
   the evidence package is never empty and a correct refusal looked identical
   to an ungrounded claim.
3. **Abstention heuristic scanned too much text.** A grounded answer ending
   "the evidence does not mention X" was misread as a refusal. Caught by its
   own test; window narrowed to the opening 200 characters.

---

## Phase 5 — Text-to-SQL ✅ COMPLETE

### Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| `TextToSQLProvider` interface | ✅ | 5 methods, `text_to_sql/provider.py` |
| `VannaTextToSQLProvider` (default) | ✅ | vanna 2.0.2 pinned, `vanna.legacy` API |
| `NativeTextToSQLProvider` (fallback) | ✅ | same schema, glossary, guard, runner |
| Vanna isolated behind our interface | ✅ | generation only; it never validates or executes |
| Schema subset retrieval, not whole schema | ✅ | 6 of 26 objects per question, hybrid scoring |
| Business glossary in the prompt | ✅ | current definitions only |
| Approved examples retrieved | ✅ | 3 most similar per question |
| sqlglot-based validation | ✅ | 46 guard tests |
| Read-only execution, audit, timeout, row cap | ✅ | `database/read_only_runner.py` |
| Tenant isolation enforced | ✅ | cross-tenant attempt blocked in practice |
| Generated SQL shown and labelled | ✅ | `scripts/query.py` |
| Optional approval mode | ✅ | `SECURITY_REQUIRE_SQL_APPROVAL` |

### ADR-002 claims, now verified in code

| Claim | Verified |
|---|---|
| Distribution is 2.0.2, not 0.7.x | ✅ `importlib.metadata.version` returns 2.0.2 |
| `vanna.__version__` is misleading | ✅ reports "0.1.0" |
| `extract_sql` truncates at `[` | ✅ overridden; regression test asserts brackets survive |

### SQL guard results

All 14 attack shapes blocked, both legitimate queries allowed:

| Attack | Result |
|---|---|
| DELETE / UPDATE / INSERT / DROP / TRUNCATE / ALTER / CREATE | BLOCK |
| `SELECT 1; DROP TABLE` (batched) | BLOCK |
| `SELECT 1 /* hide */ ; DROP TABLE` (comment-hidden) | BLOCK |
| `SELECT * INTO backup` | BLOCK |
| `EXEC sp_executesql`, `xp_cmdshell`, `OPENROWSET` | BLOCK |
| `ai.audit_events`, `security.app_users` | BLOCK |
| `password_hash` column | BLOCK |
| unqualified table name | BLOCK |
| cross-tenant `tenant_id IN (1,2,3)` | BLOCK |
| `tenant_id = 1` inside a string literal | BLOCK (parse tree, not text) |

### Live end-to-end result

"Which five customers have the highest ARR?" — both providers produced valid,
tenant-filtered T-SQL against the curated view, passed the guard, and executed
in ~35 ms. Vanna chose `vw_customer_risk`, native chose `vw_customer_360`;
both correct.

### Open gap found during testing

**A destructive request is not refused — it is silently rewritten.** Asked to
"delete all customers", the model produced an unrelated `SELECT TOP (1)
customer_id`, which the guard correctly allowed (it is a safe read) and which
then produced a misleading answer: "There is one customer in the database."

No data was at risk. But answering a destructive request with an unrelated
number is worse than refusing, because it looks like an answer. Refusal belongs
in the **router (Phase 6)**, which classifies intent before any SQL is
generated. Recorded as `SEC-001` in `evals/security_and_routing.jsonl`.

---

## Phase 6 — Agent workflow ✅ COMPLETE

### Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Router: 5 routes with structured output | ✅ | `routing/router.py`, JSON-mode classification |
| Destructive requests refused | ✅ | **by rule, before the model is consulted** — ADR-005 |
| Ambiguous questions ask for clarification | ✅ | 3 patterns, rule-based |
| Exact identifiers survive rewriting | ✅ | extracted, then re-checked; rewrite discarded if lost |
| Multi-source combines documents and SQL | ✅ | one package, `[D..]` and `[S..]` kept distinct |
| Works when Ollama is down | ✅ | deterministic heuristic fallback |
| Security tests | ✅ | 39 routing tests |

### The Phase 5 gap, closed

| | Before | After |
|---|---|---|
| Route | text_to_sql | **refuse** |
| SQL generated | `SELECT TOP (1) customer_id ...` | **none** |
| Answer | "There is one customer in the database." | "I am read-only and cannot modify the database." |

Recorded as **ADR-005**. The lesson: every layer passed its own test; the gap
was *between* them, and it took reading an answer carefully to notice.

### Multi-source, verified

Question: *"Show customers with more than three SLA breaches and summarise the
SLA policy."*

- SQL: `WHERE tenant_id = 1 AND sla_breaches > 3` — strictly greater, matching
  the wording — 15 rows
- Answer cites `[S1]` for the customer list and `[D3] [D2] [D6]` for the policy,
  each with version and effective date

### Defects found and fixed

1. **Subquestion routing was positional.** The orchestrator used
   `subquestions[-1]` as the data question; the router put *"What is the SLA
   policy?"* there, so a document question was sent to the SQL generator.
   Subquestions are now explicitly labelled `document_subquestion` /
   `data_subquestion`.
2. **The tenant rule was applied blindly.** The prompt told the model to filter
   every query on `tenant_id`, including reference tables that have no such
   column, producing `Invalid column name 'tenant_id'`. Exempt objects are now
   listed in the prompt as well as in the guard.
3. **Two router regexes were wrong**, caught by their own tests:
   `environment variable` did not match "variables", and the identifier pattern
   capped segments at 6 characters so `NW-ANALYTICS` was never extracted.

---

## Phase 7 — Quality and operations ✅ COMPLETE

### Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Trace id per request | ✅ | `observability/tracing.py` |
| Spans for every stage | ✅ | routing, retrieval, sql_generation, sql_execution, generation |
| Model, prompt and index versions recorded | ✅ | on every trace record |
| Chunk ids, scores, token counts, latencies | ✅ | span attributes |
| Credentials and PII redacted | ✅ | `observability/redaction.py` |
| Structured JSON logs | ✅ | `data/logs/copilot_YYYYMMDD.jsonl` |
| OpenTelemetry-compatible | ✅ | OTLP exporter, optional |
| Runs when the collector is down | ✅ | one warning, then local-only |
| Regression testing | ✅ | `--compare-baseline`, non-zero exit on >0.02 NDCG drop |

### Redaction, verified

| Input | Output |
|---|---|
| `DRIVER={...};UID=admin;PWD=SuperSecret123!;` | `[REDACTED CONNECTION STRING]` |
| `john.smith@northwind.example` | `j***@northwind.example` |
| `+1-555-123-4567` | `[PHONE]` |
| `Bearer eyJhbGci...` | `Bearer [REDACTED]` |

### Where the time actually goes

| stage | typical |
|---|---|
| routing (LLM) | ~4.5 s |
| dense retrieval | ~120 ms |
| sparse retrieval | ~7 ms |
| reranking (CPU) | ~2.0 s |
| SQL generation | ~15–25 s |
| SQL execution | ~35 ms |
| answer generation | ~5–12 s |

**Generation dominates; retrieval is not the bottleneck.** Worth knowing before
optimising the wrong thing.

---

## Phase 8 — Delivery ✅ COMPLETE

### Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Multipage Streamlit app | ✅ | 8 pages, bound to localhost only |
| Chat with route, SQL, citations, charts | ✅ | `app/pages/1_Copilot_Chat.py` |
| Retrieval debugger | ✅ | all four strategies side by side |
| Evaluation dashboard | ✅ | held-out results and leakage bands |
| Trace viewer | ✅ | spans, timings, redacted attributes |
| Streamlit smoke tests | ✅ | 13 tests via `AppTest` |
| Educational notebook | ✅ | 46 cells, executes top to bottom |
| Documentation | ✅ | 11 docs + 5 ADRs |
| CV-ready description | ✅ | `README.md` |
| Interview material (STAR) | ✅ | `docs/interview_guide.md` |
| Lock file | ✅ | `requirements.lock.txt`, 225 pinned |
| Repeatable setup script | ✅ | `scripts/run_all.ps1` |

### Defects found and fixed

1. **`streamlit_app.py` rendered on import.** Every page importing `sidebar`
   from it drew a second sidebar, producing `StreamlitDuplicateElementId`.
   Shared helpers moved to `app/_shared.py`, and every widget now passes an
   explicit `key`. Caught by the smoke tests.
2. **The notebook builder dropped newlines.** Source lines were stored without
   trailing `\n`, so Jupyter would have concatenated every cell into one line.
   Cell ids were also missing.
3. **Relative paths resolved against the working directory.** `.env` ships
   `QDRANT_PATH=./data/qdrant`. Running anything from another directory
   silently *created a fresh, empty* Qdrant store instead of opening the real
   one — retrieval returned nothing, with no error. Found by executing the
   notebook through nbconvert. All path settings now resolve against
   `PROJECT_ROOT`.

That third one is the most dangerous defect found in the whole build: it fails
silently and looks like a retrieval quality problem.

---

## Final state

| | |
|---|---|
| Tests | **276 passing** |
| Data validations | **43 passing** |
| Environment checks | **28 passing**, 0 failed |
| Documents | 19 (7,160 words) · 130 chunks |
| Database | ~287,000 rows · 25 tables · 7 views |
| Held-out NDCG@8 | **0.946** (+10.5 % over dense) |
| Permission/version leaks | **0** across 42 adversarial cases |
| SQL attack shapes blocked | **14/14** |
| Injection payloads obeyed | **0** |
| ADRs | 5 |

### Outstanding issues

| Issue | Impact | Owner |
|---|---|---|
| Mixed Mode disabled → no read-only SQL login | **Defence in depth incomplete**; only the application guard protects the data | Machine owner (ADR-003) |
| `TrustServerCertificate=yes` | Fine locally; must be `false` for any remote server | Deployment |
| Abstention is heuristic, not structural | Retrieval always returns top-k, so "declined" is inferred from phrasing | Needs a relevance threshold |
| Reranking hurts exact-identifier questions | 1.000 → 0.852 on `exact_code` | Needs conditional reranking |
| Embedded Qdrant is single-process | Ingestion and the app cannot run together | `QDRANT_MODE=server` |
| Query rewriting / multi-query / decomposition | Not implemented | Deferred until evaluation justifies them |
