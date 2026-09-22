# Cloudflare deployment

Deploys the Enterprise Intelligence Copilot as a public service: a React UI on
Cloudflare Pages, an API gateway on Cloudflare Workers, and the existing Python
brain in a container.

---

## Why it is shaped this way

Cloudflare Workers run V8 isolates — JavaScript, TypeScript and WASM, plus a
restricted Python via Pyodide. The copilot needs Streamlit (a long-lived
server with websockets), a native database driver, a local inference runtime,
an embedded vector store, and optionally PyTorch. **None of those run on
Workers.** Cloudflare Hyperdrive supports Postgres and MySQL, not SQL Server.

So the split is: Cloudflare owns everything at the edge — the UI, TLS,
caching, identity, rate limiting, and the only public entry point — and the
Python that already exists and is already tested runs unchanged in a
container behind it.

```
     browser
        │  https://copilot.example.com
        ▼
 ┌──────────────────────┐
 │ Cloudflare Pages     │   React 19 + Vite, static assets on the CDN
 └──────────┬───────────┘
            │  /api/*  (same origin, so no CORS preflight)
            ▼
 ┌──────────────────────┐   CORS allow-list · Access JWT verification
 │ Cloudflare Worker    │   rate limit · 64 KB body cap · streaming proxy
 └──────────┬───────────┘   holds BACKEND_TOKEN; the browser never sees it
            │  https + bearer token
            ▼
 ┌──────────────────────┐   FastAPI → the existing Copilot orchestrator:
 │ Backend container    │   router · hybrid retrieval · text-to-SQL ·
 │ (Fly / Render / CF)  │   sqlglot guard · read-only runner · tracing
 └───┬──────────┬───────┘
     │          │
     ▼          ▼
  Azure SQL  Vanna Cloud   Groq        Cloudflare
  (free)     text-to-SQL  chat        Workers AI
                                      embeddings
```

The container has no GPU, so Ollama is not available to it. Chat goes to Groq
and embeddings to Cloudflare Workers AI - see
[docs/hosted_models.md](../local-enterprise-copilot/docs/hosted_models.md),
which also covers what that discloses and what it does not.

### Why Azure SQL and not Postgres

Postgres is the more natural fit for Cloudflare — Hyperdrive supports it and
SQL Server is not supported at all. It was still the wrong choice here.

The database layer is SQL Server throughout: 2,198 lines of T-SQL across eight
scripts with ~246 dialect-specific constructs, `DIALECT = "tsql"` in the SQL
guard, T-SQL in the schema retriever's catalog queries, and ten
human-verified SQL examples. All of it is *verified* — 14 of 14 attack shapes
blocked, tenant isolation proven, 43 data validations passing.

Translating that would have put every one of those results back in question,
and it could not have been checked here: this machine has no Docker and no
local Postgres. Azure SQL's [free
offer](https://learn.microsoft.com/en-us/azure/azure-sql/database/free-offer?view=azuresql)
— 100,000 vCore-seconds and 32 GB per month, renewing for the lifetime of the
subscription — removes the only argument the other way. The dataset is ~287k
rows, far inside 32 GB.

Hyperdrive is not used. The container talks to Azure SQL directly over ODBC;
the Worker never touches the database.

The container is the only thing that can reach the database. It has a public
URL, and the bearer token is what keeps it private — which is why
`BACKEND_TOKEN` is not optional and the app refuses to serve without it.

---

## What is built and what is not

| Piece | State |
|---|---|
| `worker/` — API gateway | **Built.** Typechecks clean. Not yet deployed. |
| `ui/` — Pages front end | **Built.** Builds to 75 KB gzipped. Driven end to end against the mock. |
| `ui/mock/` — dev backend | **Built.** Working. |
| `api/` — FastAPI adapter | **Built.** Not yet run against a live copilot. |
| `api/Dockerfile` | **Written.** Not yet built — there is no Docker on this machine. |
| Vanna Cloud provider | **Experimental only.** Legacy client; excluded from the production image. |
| Database migration | **Not needed.** Azure SQL runs the existing T-SQL unchanged. |

### Still to do

- Build the image and run it once. The Dockerfile has never been built,
  because there is no Docker here.
- Provision Azure SQL and run `sql/001`–`009` against it.
- Provision server-mode Qdrant, build the index into it once, and verify that
  `/health` reports a non-zero chunk count before sending traffic.
- Rebuild the vector index against the new embedding model and re-run the
  evaluation. Changing the embedder invalidates the index *silently* - the
  vectors stop meaning anything but still return a confident top-8 - and the
  published NDCG figures stop describing the system.

---

## Prerequisites

- A Cloudflare account (free tier is enough to start).
- Node 20+ and npm. This machine has Node 24.16.0.
- A container host: [Fly.io](https://fly.io), [Render](https://render.com), or
  Cloudflare Containers (needs a Workers paid plan).
- An Azure account, for the free SQL Database offer.
- A Vanna API key only for an isolated experiment; it is not part of the
  production deployment described below.

`wrangler` is not installed globally; every command below runs it through
`npx`, which uses the version pinned in each `package.json`.

---

## 1. Authenticate

```bash
npx wrangler login
```

This opens a browser. It cannot be run from a non-interactive session, so do
it yourself before the deploy steps.

Confirm it worked:

```bash
npx wrangler whoami
```

## 2. Create the database

In the Azure portal: **Create a resource → SQL Database**. On the Basics tab,
set **Use free offer** (labelled "Apply free offer"). Name the database
`EnterpriseCopilot` to match `.env.example`.

Then, still in the portal:

- **Networking** → allow Azure services, and add your own IP so you can run
  the setup scripts from this machine.
- **Server** → note the admin login. Azure SQL has no Windows Authentication,
  so this deployment uses a SQL login, unlike the local setup.

Load the schema and the data. From `local-enterprise-copilot/`, with `.env`
pointing at Azure:

```ini
MSSQL_SERVER=yourserver.database.windows.net
MSSQL_DATABASE=EnterpriseCopilot
MSSQL_AUTH_MODE=sql
MSSQL_USERNAME=copilotadmin
MSSQL_TRUST_SERVER_CERTIFICATE=false
```

`TRUST_SERVER_CERTIFICATE` **must** be false here. It is true locally only
because the local instance uses a self-signed certificate; Azure presents a
real one, and leaving this true would accept any certificate and make the
connection interceptable.

Store the password in the Windows Credential Manager rather than in `.env`:

```powershell
.venv\Scripts\python scripts\set_secret.py
```

Then build the database. The synthetic data is generated from a fixed seed, so
this reproduces the same ~287,000 rows the evaluation was run against:

```powershell
.venv\Scripts\python scripts\setup_database.py
```

```powershell
.venv\Scripts\python scripts\generate_synthetic_data.py
```

```powershell
.venv\Scripts\python scripts\validate_data.py
```

The last command should report 43 checks, 0 failed.

### Finally fix the read-only login

`docs/architecture_decisions/ADR-003` records an unresolved issue: the local
SQL Server is Windows-auth-only, so the read-only principal in
`sql/006_create_security.sql` could never be given a login, and the
application guard was the only thing protecting the data.

**Azure SQL resolves this**, because it is SQL-auth throughout. Create a
contained user that physically cannot write:

```sql
CREATE USER copilot_app WITH PASSWORD = '<a long random password>';
ALTER ROLE copilot_readonly ADD MEMBER copilot_app;
```

Do **not** add this user to `db_datareader`: that role is broader than the
object denials configured by `sql/006_create_security.sql`. Script `009` also
enables database-enforced Row-Level Security; the runner sets the verified
tenant in SQL Server session context before every query.

Point the container at `copilot_app`, not the admin login. `check_environment.py`
then reports `[ OK ] privileges - read-only login` instead of `[WARN] this
login can WRITE`, and the guard stops being the only line of defence.

## 3. Deploy the backend container

From the **repository root**, not from `cloud/api/` — the image needs both
directories:

```bash
docker build -f cloud/api/Dockerfile -t copilot-api .
```

Then, on Fly:

```bash
fly launch --no-deploy --name copilot-api
```

```bash
fly secrets set BACKEND_TOKEN="$(openssl rand -base64 32)" COPILOT_DEMO_MODE=false COPILOT_IDENTITY_MAP_JSON='{"analyst@example.com":"analyst_na"}' MSSQL_SERVER="yourserver.database.windows.net" MSSQL_DATABASE="EnterpriseCopilot" MSSQL_AUTH_MODE=sql MSSQL_USERNAME="copilot_app" MSSQL_PASSWORD="..." MSSQL_TRUST_SERVER_CERTIFICATE=false TEXT_TO_SQL_PROVIDER=native LLM_PROVIDER=openai LLM_BASE_URL="https://api.groq.com/openai/v1" LLM_API_KEY="gsk_..." LLM_MODEL=llama-3.3-70b-versatile EMBEDDING_PROVIDER=cloudflare EMBEDDING_MODEL=@cf/baai/bge-m3 EMBEDDING_CLOUDFLARE_ACCOUNT_ID="..." EMBEDDING_CLOUDFLARE_API_TOKEN="..." QDRANT_MODE=server QDRANT_URL="https://your-cluster.qdrant.io" QDRANT_API_KEY="..." QDRANT_INDEX_VERSION="bge-m3-v1"
```

Before the first deploy, run the index builder once from a secured operator or
CI environment with the same `EMBEDDING_*` and `QDRANT_*` settings:

```bash
cd local-enterprise-copilot
python scripts/build_index.py --rebuild
python scripts/build_index.py --validate
```

Do not reuse the local qwen3 index: equal vector dimensions do not make two
embedding spaces compatible. The deployed readiness check returns 503 until
the shared Qdrant collection is populated.

```bash
fly deploy
```

Keep the `BACKEND_TOKEN` value — the Worker needs the same string.

Check it is up. This should return 401, which proves both that the service is
running and that it refuses unauthenticated callers:

```bash
curl -i https://copilot-api.fly.dev/meta
```

## 4. Deploy the Worker

Edit `cloud/worker/wrangler.toml` and set `BACKEND_URL` to the container's URL
and `ALLOWED_ORIGINS` to your Pages domain.

```bash
cd cloud/worker && npm install && npx wrangler secret put BACKEND_TOKEN
```

Paste the same token when prompted. Then:

```bash
cd cloud/worker && npx wrangler deploy
```

## 5. Deploy the UI

```bash
cd cloud/ui && npm install && npm run build
```

```bash
cd cloud/ui && npx wrangler pages deploy dist --project-name copilot-ui
```

## 6. Put them on one hostname

The UI calls `/api/*` as a relative path, so the Worker must answer on the same
hostname as the Pages site. In the Cloudflare dashboard, add a Worker route:

```
copilot.example.com/api/*  →  copilot-api
```

Same origin means no CORS preflight on every question, and no build-time API
URL to get wrong between environments.

## 7. Lock it down with Cloudflare Access

Production and staging fail closed until this step is configured: the Worker
returns 503 rather than proxying an unauthenticated request. Only local
development may set `AUTH_MODE=disabled` together with
`ENVIRONMENT=development`.

1. Zero Trust → Access → Applications → Add a self-hosted application.
2. Domain: `copilot.example.com`.
3. Add a policy — e.g. allow emails ending `@yourcompany.com`.
4. Copy the **Application Audience (AUD) tag**.
5. Set both variables in `wrangler.toml` and redeploy the Worker:

```toml
ACCESS_AUD = "your-aud-tag"
ACCESS_TEAM_DOMAIN = "yourteam.cloudflareaccess.com"
```

The Worker verifies the JWT signature against your team's JWKS. It does not
trust the `CF-Access-Authenticated-User-Email` header on its own: a request
that reaches the Worker without passing through Access can set any header it
likes.

The backend then maps the verified email to exactly one persona using
`COPILOT_IDENTITY_MAP_JSON`. The browser's `persona` field is ignored outside
demo mode, so a user cannot promote themselves to administrator. Unmapped
identities receive 403.

---

## Secrets reference

Nothing in this table belongs in a file that git can see.

| Name | Where it lives | Set with | What it is |
|---|---|---|---|
| `BACKEND_TOKEN` | Worker **and** container | `wrangler secret put` / `fly secrets set` | The shared secret that keeps the container private. Must match on both sides. |
| `MSSQL_PASSWORD` | Container | `fly secrets set` | Password for the read-only `copilot_app` user. |
| `LLM_API_KEY` | Container | `fly secrets set` | Groq key (`gsk_...`). The container has no GPU, so this is not optional. |
| `EMBEDDING_CLOUDFLARE_API_TOKEN` | Container | `fly secrets set` | Workers AI token, **Workers AI: Read** permission. |
| `MSSQL_SERVER`, `MSSQL_DATABASE`, `MSSQL_USERNAME` | Container | `fly secrets set` | Not secret, but set the same way so the connection is configured in one place. |
| `COPILOT_IDENTITY_MAP_JSON` | Container | `fly secrets set` | Email-to-persona authorization map. Treat the real employee list as sensitive. |
| `QDRANT_API_KEY` | Container / index job | `fly secrets set` | Shared vector database credential. |
| `VANNA_API_KEY` | Experimental container only | `fly secrets set` | Not used by the production profile; see `docs/vanna_cloud.md`. |
| `ACCESS_AUD` | Worker (`vars`) | `wrangler.toml` | Not secret — an identifier. |
| `ACCESS_TEAM_DOMAIN` | Worker (`vars`) | `wrangler.toml` | Not secret. |

Generate the backend token with `openssl rand -base64 32`, not by typing one.

---

## Developing locally

Three terminals, no cloud account, no database:

```bash
cd cloud/ui && node mock/server.mjs
```

```bash
cd cloud/ui && npm run dev
```

Open http://localhost:5173. Vite proxies `/api` to port 8787, where the mock
answers with realistic fixtures — including an injected tenant predicate,
conflicting policy versions, and a refusal.

To run against the real backend instead of the mock:

```bash
cd cloud/api && BACKEND_TOKEN=dev-token python main.py
```

```bash
cd cloud/worker && npx wrangler dev
```

`wrangler dev` listens on 8787, which is where Vite is already pointing.

**Wrangler does not read shell environment variables for `[vars]`.** Exporting
`BACKEND_URL` before `wrangler dev` is silently ignored and the value from
`wrangler.toml` is used instead — which means a local run quietly tries to
reach your production backend. Put local overrides in `cloud/worker/.dev.vars`
(gitignored):

```ini
BACKEND_TOKEN=dev-token
BACKEND_URL=http://localhost:8000
ALLOWED_ORIGINS=http://localhost:5173
ENVIRONMENT=development
AUTH_MODE=disabled
```

Run the backend with `COPILOT_DEMO_MODE=true` for this local persona-switching
demo. Staging and production use `AUTH_MODE=required`; a missing Access AUD or
team domain then fails closed with 503 instead of silently becoming public.

To skip the Worker entirely and drive the backend directly:

```bash
cd cloud/ui && COPILOT_API=http://127.0.0.1:8000 BACKEND_TOKEN=dev-token npm run dev
```

### One process at a time owns the index

Embedded Qdrant permits a single client per storage folder. That has three
consequences you will meet:

- **Stop the backend before running the test suite.** Both want the same
  folder, and the second one to ask gets "already accessed by another
  instance of Qdrant client".
- **The container runs one uvicorn worker.** A second worker in the same
  container opens a second client and one of them silently gets nothing.
  Scale with more containers.
- **Anything reading the index inside the backend must borrow the copilot's
  client**, not open its own. `/health` and `/meta` originally opened their
  own and reported a healthy 130-chunk index as broken.

`QDRANT_MODE=server` against a shared Qdrant removes all three.

### What has been verified locally, and what has not

The Worker was run under `wrangler dev` and exercised:

| Behaviour | Result |
|---|---|
| Unknown route | its own 404, never proxied |
| CORS, allowed origin | `Access-Control-Allow-Origin` + credentials, `Vary: Origin` |
| CORS, disallowed origin | **no** allow-origin header, so the browser blocks it |
| Body over 64 KB | 413, before any proxying |
| Backend unreachable | clean 503, and the backend URL is not leaked |
| `.dev.vars` bindings | loaded, including the rate limiter |

**The proxy hop itself and Access JWT verification are not verified.**
`workerd` has no outbound network in the environment this was built in —
miniflare could not even fetch its own `Request.cf` object — so every
`fetch()` to the backend failed regardless of the URL, while `curl` to the
same address returned 200. That is an environment restriction, not a defect,
but it does mean the first real proxied request will happen on your machine
and not on this one. Run the table above again after `npx wrangler dev` on
your side, and `GET /api/health` should return the backend's JSON.

---

## Cost

| | Free tier | Notes |
|---|---|---|
| Pages | unlimited static requests | The UI costs nothing. |
| Workers | 100k requests/day | The gateway is well inside this. |
| Azure SQL | 100k vCore-sec + 32 GB/month, for the lifetime of the subscription | The dataset is ~287k rows, far inside it. |
| Fly.io | ~$2–5/month | The only guaranteed cost. A free-tier alternative sleeps, and a cold start on this app is slow. |
| Vanna Cloud | free tier | Check current limits. |

The container is the expensive part, and it is expensive because it is the
part that cannot be serverless.
