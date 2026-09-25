# Database schema

The structured half of the copilot: a synthetic but realistic B2B SaaS
company ("Northwind Cloud") with customers, subscriptions, billing, support
and incidents across three tenants. The unstructured half (policies, SLAs,
incident post-mortems) lives in `local-enterprise-copilot/data/documents` and
refers to the same customers, plans and incidents. That shared world is what
makes hybrid questions ("customers with more than three SLA breaches, and what
does the SLA policy say?") answerable.

## One schema, three engines

| Backend (`DATABASE_BACKEND`) | Used by | Built from | Read-only enforcement |
|---|---|---|---|
| `duckdb` (default for local and cloud) | `run_local.py`, Streamlit Cloud, Docker image | `sql/postgres/*.sql`, translated, plus the synthetic generator (`scripts/build_duckdb.py`) | File opened `read_only=True`: the engine refuses every write |
| `postgresql` | Neon or any PostgreSQL 16 | `sql/postgres/*.sql` (`scripts/setup_database.py`) | `copilot_readonly` role + row-level security (`006`, `009`) |
| `sqlserver` | Local SQL Server / SSMS | `sql/*.sql` (T-SQL source of truth) | `copilot_readonly` role, `DENY` on `ai`/`security` (ADR-003) |
| `none` | documents-only | – | – |

The PostgreSQL scripts are generated from the T-SQL ones
(`scripts/translate_tsql_to_postgres.py`), and the DuckDB database is built
from the PostgreSQL scripts (`database/duckdb_store.py`). So there is one
schema with one generator. The synthetic data is deterministic
(`COPILOT_SEED`), so the three engines hold the same rows.

**DuckDB translation**, done mechanically at build time:
- `GENERATED ALWAYS AS IDENTITY` becomes a sequence default.
- Cross-schema foreign keys are dropped (DuckDB cannot declare them) and
  recorded in `ai.schema_relationships`, so Text-to-SQL still learns the
  join paths. The PostgreSQL CI job enforces the real constraints on the
  same generator output.
- `DO` blocks, `GRANT`/`REVOKE` and view `security_invoker` options are
  removed. There are no users in an embedded file; tenant isolation comes
  from the guard's injected predicate (see Security below).
- Indexes are skipped: DuckDB is columnar and does not need them at this
  size.

Build: `local-enterprise-copilot/.venv/Scripts/python local-enterprise-copilot/scripts/build_duckdb.py [--demo|--full] [--rebuild]`.
It takes about 18 s for the full dataset (24 MB) and about 3 s for demo.

## Schemas

| Schema | Contents | Readable by generated SQL? |
|---|---|---|
| `core` | tenants, customers, contacts, products, plans, subscriptions, subscription changes, daily usage | yes |
| `billing` | invoices, invoice items, payments, refunds | yes |
| `support` | tickets, ticket events, SLA policies, SLA breaches, incidents, incident impact | yes |
| `analytics` | curated views + health snapshots | yes, **preferred** |
| `security` | app users, access groups | **no** |
| `ai` | glossary, golden SQL, audit trail, schema relationships | **no** |

`ai` and `security` are outside the guard's allow-list
(`SECURITY_ALLOWED_SCHEMAS`) and the schema catalog: the model is never told
they exist, and a query naming them is blocked.

## Tables (full dataset, `COPILOT_DEMO_MODE=false`)

| Table | Rows | Key columns |
|---|---:|---|
| `core.tenants` | 3 | tenant_id, tenant_code (NWC-NA, NWC-EU, NWC-APAC), region, default_currency |
| `core.customers` | 600 | customer_id, tenant_id, display_name, segment, region, signup_date, churn_date, status |
| `core.customer_contacts` | 1,189 | contact_id, customer_id, full_name, email (synthetic) |
| `core.products` | 5 | product_id, product_code, product_family |
| `core.plans` | 20 | plan_id, product_id, tier, billing_interval, list_price_monthly, effective_from/to |
| `core.subscriptions` | 907 | subscription_id, customer_id, plan_id, status, seats, mrr_amount, discount_pct |
| `core.subscription_changes` | 1,386 | change_type (new/upgrade/downgrade/seat_change/cancel/reactivate), mrr_delta |
| `core.usage_daily` | 236,992 | customer_id, product_id, usage_date, active_users, sessions, api_calls |
| `billing.invoices` | 8,689 | invoice_id, customer_id, issue_date, due_date, total_amount, amount_paid, status, paid_date |
| `billing.invoice_items` | 8,689 | invoice_id, product_id, plan_id, quantity, unit_price, line_amount |
| `billing.payments` | 8,011 | invoice_id, payment_date, amount, method, status, days_late |
| `billing.refunds` | 279 | invoice_id, refund_date, amount, reason_code, is_partial |
| `support.tickets` | 4,000 | ticket_id, customer_id, priority, category, opened/first_response/resolved_at_utc, satisfaction_score |
| `support.ticket_events` | 11,210 | ticket_id, event_type, actor_type |
| `support.sla_policies` | 24 | plan_tier, priority, first_response_minutes, resolution_minutes, version, is_current |
| `support.sla_breaches` | 712 | ticket_id, customer_id, breach_type, target/actual/breach_minutes, credit_issued |
| `support.incidents` | 9 | incident_code (e.g. INC-2025-0042), severity, root_cause, postmortem_doc_id |
| `support.incident_impact` | 490 | incident_id, customer_id, impact_level, downtime_minutes, credit_amount |
| `analytics.customer_health` | 4,767 | customer_id, snapshot_date, health_score, risk_band, churn_risk_pct |

Every business table carries `tenant_id`. Money is `DECIMAL(19,4)`,
business days are `DATE`, and points in time are `TIMESTAMP(3)` in UTC
(`*_utc`).

## Analytics views (the preferred surface for generated SQL)

| View | Grain | Use for |
|---|---|---|
| `analytics.vw_customer_360` | customer | ARR/MRR, tenure, open/overdue invoices, tickets, SLA breaches, health, risk |
| `analytics.vw_monthly_recurring_revenue` | tenant × month | MRR, ARR, paying customers, seats |
| `analytics.vw_churn_metrics` | tenant × month | churned customers/MRR, expansion, contraction, new business |
| `analytics.vw_sla_performance` | ticket | target vs actual response/resolution, breach flags |
| `analytics.vw_customer_risk` | customer | risk signals (unpaid, repeated breaches, low usage, low health) |
| `analytics.vw_incident_impact` | incident × customer | affected customers, downtime, credits |
| `analytics.vw_month_spine` | month | calendar helper (hidden from the model) |

## Relationships

42 foreign keys: every child → `core.tenants`, `core.customers` or its
parent. The main join paths:

```
core.tenants ─┬─< core.customers ─┬─< core.subscriptions ─< core.subscription_changes
              │                   ├─< billing.invoices ─┬─< billing.invoice_items
              │                   │                     ├─< billing.payments ─< billing.refunds
              │                   ├─< support.tickets ─┬─< support.ticket_events
              │                   │                    └─< support.sla_breaches >─ support.sla_policies
              │                   ├─< support.incident_impact >─ support.incidents
              │                   ├─< core.usage_daily >─ core.products ─< core.plans
              │                   └─< analytics.customer_health
```

The full list is in `ai.schema_relationships` (DuckDB) or the catalog's
foreign keys (PostgreSQL/SQL Server), and is fed to Text-to-SQL as
"Join relationship" documentation.

## Semantic layer (`ai` schema, never readable by generated SQL)

- **`ai.business_glossary`**: 19 current definitions, each with SQL guidance
  and explicit exclusions: MRR, ARR, Active customer, Active subscription,
  New business, Expansion/Contraction revenue, Churned customer, Revenue churn,
  Logo churn, Trial/Paid customer, Overdue invoice, Refund rate, SLA breach,
  First-response time, Resolution time, At-risk customer, Product adoption.
  The definitions most relevant to a question go into the SQL prompt.
- **`ai.approved_sql_examples`**: 10 human-verified "golden" queries (for
  example "Which five customers have the highest ARR?", "Why did churn
  increase in Q2?", "Show customers with more than three SLA breaches"). They
  are the few-shot examples for the native generator and Vanna's
  question→SQL training set. `tests/test_duckdb_backend.py` runs every one on
  DuckDB.
- **`ai.audit_events`**: every generated query, written *before* execution
  on SQL Server/PostgreSQL. On read-only DuckDB the same event goes to the
  structured log.

## Deliberate data patterns (for evaluation)

Q4 new business is about 40% above baseline. Q2-2025 churn spikes about
2.5× after the June SEV1 incident (INC-2025-0042). About 18% of invoices
are paid late and 6% are unpaid. About 40% of refunds are partial. There are
8 near-duplicate customer names, plus realistic NULLs (industry about 12%,
satisfaction about 55%). See `EDGE_CASES` in `database/synthetic.py`.

## Sample queries (PostgreSQL dialect, as the model writes them)

```sql
-- Top five customers by ARR (tenant predicate is injected by the guard)
SELECT customer_name, current_arr, segment, region
FROM analytics.vw_customer_360
WHERE customer_status = 'active' AND tenant_id = 1
ORDER BY current_arr DESC
LIMIT 5;

-- Monthly revenue from paid invoices in 2025
SELECT date_trunc('month', paid_date)::date AS month_start, SUM(total_amount) AS revenue
FROM billing.invoices
WHERE tenant_id = 1 AND status = 'paid'
  AND paid_date >= '2025-01-01' AND paid_date < '2026-01-01'
GROUP BY month_start ORDER BY month_start;

-- Customers with repeated SLA breaches (hybrid: paired with the SLA policy document)
SELECT customer_name, segment, sla_breaches, current_arr
FROM analytics.vw_customer_360
WHERE sla_breaches > 3 AND tenant_id = 1
ORDER BY sla_breaches DESC;
```

## Security of the SQL path (every backend)

1. **Router refusal rules**: destructive or exfiltration intent is refused
   before any model is asked.
2. **SQL guard** (`security/sql_guard.py`, sqlglot AST): a single `SELECT`
   only. It blocks `DROP/DELETE/TRUNCATE/INSERT/UPDATE/ALTER/MERGE/EXEC`,
   multiple statements, non-allow-listed schemas, blocked columns and too many
   joins. It **injects the tenant predicate** when missing and adds a row
   limit.
3. **Execution**: timeout, row cap (`MSSQL_MAX_RESULT_ROWS`), column
   redaction, and an audit event.
4. **Engine**: a read-only role (SQL Server/PostgreSQL) or a read-only file
   handle (DuckDB).
5. **Self-correction**: when the database rejects a guard-approved query,
   its error message is sent back to the model for a corrected query, at most
   `SQL_EXECUTION_RETRIES` (default 2) times. **The corrected query passes
   through the guard again.** A query the guard *blocked* is never retried.
