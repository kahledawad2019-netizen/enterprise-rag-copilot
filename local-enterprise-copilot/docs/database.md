# Database

**Database:** `EnterpriseCopilot` · **Schema version:** 1.0.0
**Target:** SQL Server 2019+ (developed against 2025 Express)

## Run order

Scripts are idempotent and safe to re-run.

| # | Script | Creates |
|---|---|---|
| 001 | `001_create_database.sql` | database, recovery model, RCSI |
| 002 | `002_create_schemas.sql` | core, billing, support, analytics, security, ai |
| 003 | `003_create_tables.sql` | 25 tables, 42 FKs, 27 CHECK constraints |
| 004 | `004_create_indexes.sql` | 29 indexes, tenant-leading |
| 005 | `005_create_views.sql` | 7 analytics views |
| 006 | `006_create_security.sql` | `copilot_readonly` role, 38 permissions |
| 007 | `007_seed_reference_data.sql` | tenants, products, plans, SLA policies, glossary |
| 008 | `008_validation_queries.sql` | 43 assertions |

**Automated:** `python scripts\setup_database.py`
**In SSMS:** open each file in order, execute against `EnterpriseCopilot`
(except 001, which runs against `master`).

## Schemas, and why the split matters

| Schema | Contents | Readable by generated SQL? |
|---|---|---|
| `core` | customers, products, plans, subscriptions, usage | yes |
| `billing` | invoices, items, payments, refunds | yes |
| `support` | tickets, SLA policies, breaches, incidents | yes |
| `analytics` | curated views, health snapshots | yes — **preferred** |
| `security` | app users, access groups | **no** |
| `ai` | glossary, approved examples, audit trail | **no** |

`ai` and `security` are excluded from the schema catalog *and* from the SQL
guard's allow-list. The model is never told they exist, and could not read them
if it guessed. It may `INSERT` into `ai.audit_events` but is explicitly
`DENY SELECT` on it — being able to read its own audit trail would let a
prompt-injected model learn which attempts were blocked and why.

## Analytics views

Text-to-SQL is steered towards these for three reasons: they encode the
business definitions once (so the model cannot invent its own MRR formula),
they expose no sensitive columns, and they replace five-table joins the model
would otherwise get subtly wrong.

| View | Answers |
|---|---|
| `vw_customer_360` | one row per customer: ARR, tenure, tickets, breaches, health |
| `vw_monthly_recurring_revenue` | MRR/ARR per tenant per month, trials excluded |
| `vw_churn_metrics` | logo and revenue churn, expansion, contraction |
| `vw_sla_performance` | target vs actual, **using the SLA version in force when the ticket opened** |
| `vw_customer_risk` | at-risk customers with the individual signals |
| `vw_incident_impact` | incidents joined to affected customers and credits |
| `vw_month_spine` | internal calendar helper — hidden from the model |

### The subtlety in `vw_sla_performance`

A ticket from 2024 must be judged against SLA v1.0 (30-minute Enterprise P1),
not v2.0 (15 minutes). The view resolves the policy by the ticket's open date.
Joining on `is_current = 1` would silently misreport every pre-2025 ticket, and
that is the single most common reporting error in this domain.

## Design choices

**Money is `DECIMAL(19,4)`, never `FLOAT`.** Binary floating point cannot
represent 0.10 exactly and revenue totals must reconcile.

**Timestamps are `DATETIME2(3)` in UTC, named `*_utc`.** Explicit naming is
what makes the deliberate 23:00-UTC tickets a meaningful test rather than an
ambiguity.

**Optional columns are genuinely NULLable.** ~12 % of `industry` and ~55 % of
`satisfaction_score` are NULL, because real CRM data has holes and code that
assumes otherwise breaks in production, not in the demo.

**`mrr_amount` is stored, not derived.** It captures the negotiated price after
discount, which cannot be recovered from the plan list price.

## Synthetic data

Deterministic from `COPILOT_SEED` (default 20240601). Same seed, same data,
byte for byte — which is what makes the evaluation assertions valid.

| Table | Rows |
|---|---|
| core.customers | 600 |
| core.subscriptions | 907 |
| core.usage_daily | 236,992 |
| billing.invoices | 8,689 |
| support.tickets | 4,000 |
| support.sla_breaches | 712 |
| **total** | **~287,000** |

16 deliberate patterns are documented in
`src/enterprise_copilot/database/synthetic.py::EDGE_CASES` and asserted by
`sql/008_validation_queries.sql`.

## Known facts the evaluation depends on

| Fact | Value |
|---|---|
| Customers affected by INC-2025-0042 | 99 |
| Customers with more than 3 SLA breaches | 31 |
| At-risk customers (2+ signals) | 127 |
| Total active MRR | $531,758 |
| Late payment rate | 18.2 % |

Changing the seed changes these, and `evals/text_to_sql.jsonl` must be
regenerated if you do.
