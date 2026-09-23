# Security and Deployment Audit

Date: 2026-09-22  
Branch reviewed: `cloud-deploy`  
Original review range: `3631618..0b568a3`

## Verdict

**Ready for a controlled staging deployment after cloud resources are
provisioned. Not ready for production traffic.**

The Cloudflare Pages → Worker → Python container split is sound for this
project. Cloudflare Python Workers can now run FastAPI and supported
Pyodide/PyEmscripten packages, but this application still needs Microsoft ODBC
and other native/runtime behavior that belongs in a container. Cloudflare
Containers can host that container on a Workers Paid plan; Fly or Render is the
simpler lower-cost backend option while Pages and the security gateway remain
on Cloudflare.

Recommended staging topology:

```text
Browser
  -> Clerk authentication
  -> Cloudflare Pages (React)
  -> Cloudflare Worker (JWT verification, CORS, rate/body limits)
  -> FastAPI container (server-side authorization and orchestration)
  -> Azure SQL (read-only principal + Row-Level Security)
  -> Qdrant server/cloud (versioned document index)
  -> hosted chat API + Cloudflare Workers AI embeddings
```

## Critical findings and remediation

### 1. Browser-controlled role escalation — fixed

The backend previously trusted a browser-provided `persona`, so any
authenticated user could request the administrator persona. The Worker now
passes a verified email and the backend maps that identity to one fixed persona
using `COPILOT_IDENTITY_MAP_JSON`. Persona switching remains available only
when `COPILOT_DEMO_MODE=true`. Unmapped identities receive 403, and traces are
administrator-only.

Regression tests cover production mapping, attempted administrator override,
unmapped users, and demo-only switching.

### 2. Worker authentication fail-open — fixed

Production and staging now default to `AUTH_MODE=required` and return 503 when
the Access audience or team domain is missing. Authentication can be disabled
only when `ENVIRONMENT=development`. The Worker validates signature, `alg`,
issuer, audience, expiry, not-before and email claims. Malformed signatures are
handled as authentication failures. The request body is streamed into a 64 KiB
cap rather than fully materialized before checking its size.

Cloudflare explicitly requires the Access JWT to be validated at the Worker or
origin, even when Access is in front of it:
https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/

### 3. SQL tenant-isolation bypass — fixed in code; database verification pending

The previous validator accepted a matching equality anywhere in the
expression, so `tenant_id = 1 OR 1 = 1` and
`tenant_id = 1 OR tenant_id = 2` passed. A join could also scope only its first
tenant table.

The SQL guard now requires an unweakened equality for every tenant-scoped
relation, rejects missing verified tenant context, and revalidates rewritten
SQL. Tenant injection scopes every relevant join relation. The database runner
sets or clears SQL Server `SESSION_CONTEXT('tenant_id')` on every borrowed
connection and fetches at most `max_result_rows + 1` rows instead of calling
`fetchall()`.

`sql/009_enable_row_level_security.sql` adds the database-enforced filter
policy. The deployment principal is created outside source control and is added
only to `copilot_readonly`; it must never be `db_owner`, `db_datareader` or
`sysadmin`.

Still required: execute scripts 001–009 in Azure SQL, run the privilege checks
as the application principal, and prove cross-tenant reads return no rows.

### 4. Empty/stale vector index deployment — blocked explicitly

The Docker image does not bake in a local index. Production documentation now
requires server-mode Qdrant and a one-off index build using the exact deployed
embedding provider/model and a new `QDRANT_INDEX_VERSION`. `/health` is a
readiness check and returns 503 while the index, database or model is
unavailable, so an empty container cannot be marked ready.

The old qwen3 vectors cannot be reused with bge-m3 even though both dimensions
are 1024. Equal dimensions do not mean compatible vector spaces.

### 5. Misleading evaluation page — fixed

Hardcoded summary numbers were replaced by a committed, versioned baseline
artifact. The API compares its evaluated provider/model/index version with the
live configuration. The UI presents a prominent historical-baseline warning
when they differ. The exact latest reranked-vs-hybrid result is now
`delta=0.021413`, `p=0.2256`, rather than the stale rounded copy.

The Cloudflare/bge-m3 index must be evaluated again before release. Historical
qwen3 scores are evidence about the old system, not the deployed system.

### 6. Vanna Cloud — isolated as experimental

The current adapter uses `vanna.legacy`, not the V2 Agent API. The open-source
repository was archived on 2026-03-29, and Vanna's current security page
describes the premium backend as development/demo software. The production
requirements therefore exclude Vanna and use `TEXT_TO_SQL_PROVIDER=native`.

The experimental overlay remains available for a separate smoke test. Request
handling no longer auto-uploads DDL, glossary or SQL examples. A failed remote
training-state check now fails closed instead of assuming an empty corpus and
re-exporting data.

Sources:

- https://github.com/vanna-ai/vanna
- https://vanna.ai/data-security
- https://vanna.ai/docs/migration

Do not enable Vanna Cloud with real company data until support ownership, DPA,
retention, deletion, encryption and live API behavior are confirmed in writing.

### 7. Repository and image hygiene — fixed

- A broad `models/` ignore rule hid the real
  `src/enterprise_copilot/models` package. The rule is now root-scoped and the
  source files are visible to Git.
- A whitelist `.dockerignore` prevents `.env`, virtual environments, local
  indexes, traces and Git history from entering remote build contexts.
- Production dependencies are compiled into `cloud/api/requirements.lock.txt`.
- Vanna/Chroma are absent from the production image.
- The checked-in predictable SQL login password was removed.

The new/untracked source files must be staged before the first commit. A clean
clone should then be tested; otherwise local success can still hide an omitted
file.

## Verification completed

| Gate | Result |
|---|---|
| Deterministic Python tests | **351 passed, 54 deselected** |
| Targeted security tests | **81 passed, 4 SQL Server skips** |
| Ruff | **pass** |
| Mypy | **pass: 52 source files** |
| Python coverage | **49% total** |
| Worker TypeScript | **pass** |
| React production build | **pass**, about 81 KiB gzip JS |
| Worker npm production audit | **0 known vulnerabilities** |
| UI npm production audit | **0 known vulnerabilities** |
| Locked production Python audit | **0 known vulnerabilities** |
| Diff whitespace/integrity | **pass** |

The four targeted skips require SQL Server/ODBC and are correctly reported as
skips, not passes.

Coverage remains weak in important orchestration paths: orchestrator 24%,
tracing 27%, hybrid retrieval 33%, answerer 33%, vector store 32%. This does
not invalidate the passing security tests, but it is a production quality gap.

## Mandatory live release gates

All items below are **No-Go** until proven in staging:

1. Build the Docker image. It has not been built on this machine.
2. Provision Azure SQL; run scripts 001–009; verify the application principal,
   RLS and tenant isolation using real connections.
3. Provision Qdrant server/cloud; build and validate the bge-m3 index; confirm a
   non-zero chunk count and correct permission filtering.
4. Configure Clerk and exercise valid, missing, malformed, expired,
   bad-signature, wrong-origin, wrong-AUD and unmapped-user JWTs through the
   real Worker-to-backend hop.
5. Run end-to-end questions through document RAG, Text-to-SQL and multi-source
   routes with the selected hosted model.
6. Re-run the held-out document evaluation against the deployed index.
7. Run Text-to-SQL evaluation against Azure SQL and require 100% SQL safety,
   100% tenant isolation and agreed execution/correctness thresholds.
8. Configure durable OTLP/log storage. Local JSONL traces are ephemeral and do
   not form production observability across multiple containers.
9. Add load/concurrency tests. The API currently serializes copilot access to
   protect a process-global tracer; it is safe but limits throughput.
10. Raise unit coverage on the orchestrator, tracing, retrieval and answer
    generation paths, with a risk-based target rather than relying on the
    aggregate percentage alone.

## Secrets and configuration required from the owner

Do not paste secret values into chat or commit them. Enter them directly into
the relevant platform secret store.

Non-secret decisions/identifiers needed:

- production and staging domains;
- Clerk issuer/JWKS URL, application audience and exact authorized UI origins;
- email-to-persona mappings;
- chosen container host (recommended first: Fly/Render; Cloudflare Containers
  if a Workers Paid plan and Cloudflare-only hosting are desired);
- Azure SQL server/database/application username;
- Qdrant endpoint and index version;
- selected hosted chat model and Cloudflare embedding model.

Secrets entered only through platform CLIs/dashboards:

- `BACKEND_TOKEN` on both Worker and container;
- `MSSQL_PASSWORD`;
- `LLM_API_KEY`;
- `EMBEDDING_CLOUDFLARE_API_TOKEN`;
- `QDRANT_API_KEY`;
- `COPILOT_IDENTITY_MAP_JSON` (contains employee identities and authorization
  roles, so treat it as sensitive configuration).

## Platform references

- Python Workers and supported package model:
  https://developers.cloudflare.com/workers/languages/python/
- Pyodide/Worker runtime behavior:
  https://developers.cloudflare.com/workers/languages/python/how-python-workers-work/
- Cloudflare Containers (Workers Paid):
  https://developers.cloudflare.com/containers/
- React on Cloudflare Pages:
  https://developers.cloudflare.com/pages/framework-guides/deploy-a-react-site/

