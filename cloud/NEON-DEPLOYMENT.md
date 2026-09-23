# Neon PostgreSQL deployment

This is the current database runbook for the Cloudflare deployment. Never put
a real DSN, API key, password, or backend token in Git.

## What has already been verified

- PostgreSQL migrations `001` through `009` execute and re-execute on a clean
  PostgreSQL 16 server in GitHub Actions.
- All analytics views are `security_invoker`, so their underlying reads do not
  bypass the caller's row-level-security policies.
- Fifteen tenant tables have both `ENABLE ROW LEVEL SECURITY` and
  `FORCE ROW LEVEL SECURITY` plus a `tenant_isolation` policy.
- `copilot_readonly` is `NOLOGIN`, `NOSUPERUSER`, `NOCREATEDB`,
  `NOCREATEROLE`, and `NOBYPASSRLS`.
- The role can read approved business data and append audit events, but cannot
  write business data or read `security.app_users` / `ai.audit_events`.
- Python selects the PostgreSQL dialect, emits `LIMIT`, uses `psycopg`, and
  sets `statement_timeout` and `app.tenant_id` transaction-locally.

These checks prove the code and an ephemeral PostgreSQL 16 server. They do not
prove a real Neon role, network path, secret store, or production dataset;
those live gates are below.

## 1. Create the Neon project and keep two DSNs

Create one project/database in Neon. Use two separate credentials:

- `migration owner`: only for schema migrations and initial data loading;
- `copilot_app`: the deployed, non-owner application login.

Use the direct (non-pooler) owner DSN for migrations. Use Neon's pooled DSN for
the API if available. Both must require TLS. Store them in your shell or secret
manager, never in `.env.example`.

Local operator environment for the initial build:

```ini
DATABASE_BACKEND=postgresql
POSTGRES_DSN=postgresql://OWNER:SECRET@DIRECT_HOST/DATABASE?sslmode=require
TEXT_TO_SQL_PROVIDER=native
```

## 2. Create schema, load data, then enable RLS

Run from `local-enterprise-copilot/`:

```powershell
.\.venv\Scripts\python.exe scripts\setup_database.py --defer-rls
.\.venv\Scripts\python.exe scripts\generate_synthetic_data.py
.\.venv\Scripts\python.exe scripts\setup_database.py --only 009
```

The order is intentional. `009` forces RLS even for the table owner. Loading a
multi-tenant dataset after that without an explicit tenant context must fail.

Run the validation query after data loading:

```powershell
.\.venv\Scripts\python.exe scripts\setup_database.py --validate
```

## 3. Provision the application login

Generate the password in a password manager. Run as the migration owner:

```sql
CREATE ROLE copilot_app LOGIN PASSWORD '<generated secret>'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT copilot_readonly TO copilot_app;
```

If Neon requires roles to be created in its console, create `copilot_app`
there, then run only the `GRANT`. Do not make it the database or schema owner.

Create the production DSN from this role and store it only in the backend
host's secret manager:

```text
DATABASE_BACKEND=postgresql
POSTGRES_DSN=postgresql://copilot_app:SECRET@POOLER_HOST/DATABASE?sslmode=require
```

## 4. Mandatory live database gates

Run these using the `copilot_app` DSN, not the owner DSN:

1. `/health` reports the database reachable.
2. The connected role is not superuser, owner, or `BYPASSRLS`.
3. With `app.tenant_id` set to the EMEA tenant, `core.tenants` exposes exactly
   that tenant and no other tenant.
4. With tenant context absent, tenant tables expose zero rows.
5. `DELETE FROM core.customers` fails.
6. `SELECT * FROM security.app_users` fails.
7. `SELECT * FROM ai.audit_events` fails.
8. An audit `INSERT` succeeds.
9. A query through `analytics.vw_customer_360` cannot expose another tenant.

Never treat a successful owner connection as a security test: owners,
superusers, and `BYPASSRLS` roles can bypass the boundary being tested.

## 5. Backend secrets

Set these on the Python container host:

```text
DATABASE_BACKEND=postgresql
POSTGRES_DSN=<copilot_app pooled TLS DSN>
TEXT_TO_SQL_PROVIDER=native
BACKEND_TOKEN=<random 32+ byte secret>
COPILOT_IDENTITY_MAP_JSON=<verified email to persona mapping>
LLM_PROVIDER=openai
LLM_BASE_URL=<provider endpoint>
LLM_API_KEY=<secret>
LLM_MODEL=<model id>
EMBEDDING_PROVIDER=cloudflare
EMBEDDING_CLOUDFLARE_ACCOUNT_ID=<account id>
EMBEDDING_CLOUDFLARE_API_TOKEN=<secret>
EMBEDDING_MODEL=@cf/baai/bge-m3
QDRANT_MODE=server
QDRANT_URL=<shared Qdrant endpoint>
QDRANT_API_KEY=<secret>
QDRANT_INDEX_VERSION=bge-m3-v1
```

Do not set `VANNA_API_KEY` in the production profile yet. Vanna Cloud remains
an opt-in experiment until its data egress, retention, support status, and
quality are revalidated with the live account.

## 6. Cloudflare edge

The Worker receives the same `BACKEND_TOKEN` as a Worker secret. Configure
Clerk JWT verification (`CLERK_ISSUER`, `CLERK_JWKS_URL`, `CLERK_AUDIENCE`
and `CLERK_AUTHORIZED_PARTIES`) before public traffic. The browser receives
only Clerk's publishable key and short-lived session token; it never receives
the backend token or database DSN.

After deploying the backend, rebuild the vector index with the exact production
embedding configuration, run retrieval/Text-to-SQL evaluation, then deploy the
Worker and Pages UI. A green schema migration is necessary but is not an
end-to-end RAG release gate.
