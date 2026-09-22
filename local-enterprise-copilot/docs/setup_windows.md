# Setup on Windows

Tested end to end on Windows 11, Python 3.13, SQL Server 2025 Express.

## 1. Prerequisites

| Requirement | Check | If missing |
|---|---|---|
| Python 3.11-3.13 | `py -0` | [python.org](https://www.python.org/downloads/) — **not 3.14**, some wheels do not exist yet |
| SQL Server Database Engine | `Get-Service MSSQL*` | see below |
| ODBC Driver 18 | `Get-OdbcDriver -Platform 64-bit` | [Microsoft ODBC Driver](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server) |
| Ollama | `ollama --version` | [ollama.com](https://ollama.com/download) |

> **SSMS is not the database engine.** Installing SQL Server Management Studio
> gives you a client, not a server. `Get-Service MSSQL*` returning nothing means
> the engine is not installed.

### Installing SQL Server

**Express** (free, 10 GB limit — sufficient for this project):
[Download SQL Server Express](https://www.microsoft.com/sql-server/sql-server-downloads)
Choose *Basic*. The default instance name is `SQLEXPRESS`.

**Developer** (free, no limits, not licensed for production): same page.

**Container alternative**, if you prefer not to install the engine:

```powershell
docker run -e "ACCEPT_EULA=Y" -e "MSSQL_SA_PASSWORD=<StrongPassword>" `
  -p 1433:1433 -d mcr.microsoft.com/mssql/server:2022-latest
```

Then set `MSSQL_SERVER=localhost,1433`, `MSSQL_AUTH_MODE=sql`,
`MSSQL_USERNAME=sa` in `.env`.

## 2. Project setup

```powershell
cd local-enterprise-copilot

py -3.13 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[dev,rerank,vanna]"

Copy-Item .env.example .env
```

Install extras selectively if disk is tight:

| Extra | Size | What you lose without it |
|---|---|---|
| `dev` | small | tests, linting |
| `rerank` | ~2.5 GB (PyTorch) | reranking degrades to a no-op, ~2 % NDCG |
| `vanna` | ~200 MB | the Vanna provider; native still works |
| `phoenix` | ~150 MB | the trace UI; local JSONL traces still work |

## 3. Local models

```powershell
ollama pull llama3.1:8b          # ~4.9 GB
ollama pull qwen3-embedding:0.6b # ~640 MB
```

Lower-spec machines: set `COPILOT_PROFILE=lite` in `.env` and
`ollama pull llama3.2:3b nomic-embed-text` instead.

## 4. Verify before building

```powershell
.venv\Scripts\python scripts\check_environment.py
```

28 checks. It must report **0 failed** before continuing. Warnings about
`TrustServerCertificate` and the sysadmin login are expected locally.

## 5. Database

```powershell
.venv\Scripts\python scripts\setup_database.py           # scripts 001-007
.venv\Scripts\python scripts\generate_synthetic_data.py  # ~287k rows, ~10 s
.venv\Scripts\python scripts\validate_data.py            # 43 assertions
```

Prefer SSMS? See `docs/database.md` for the manual run order.

## 6. Document index

```powershell
.venv\Scripts\python scripts\build_index.py --rebuild
```

~13 s for 130 chunks. Re-running without `--rebuild` skips unchanged documents.

## 7. Run it

```powershell
# command line
.venv\Scripts\python scripts\copilot.py "what is the refund policy for annual plans?"

# UI (bound to localhost only)
.venv\Scripts\streamlit run app\streamlit_app.py

# tests
.venv\Scripts\python -m pytest -q
```

## Command reference

| Task | Command |
|---|---|
| Health check | `python scripts\check_environment.py` |
| Build database | `python scripts\setup_database.py` |
| Regenerate data | `python scripts\generate_synthetic_data.py` |
| Validate data | `python scripts\validate_data.py` |
| Rebuild index | `python scripts\build_index.py --rebuild` |
| Incremental index | `python scripts\build_index.py` |
| Search only | `python scripts\search.py --compare "query"` |
| Document answer | `python scripts\ask.py "question"` |
| SQL answer | `python scripts\query.py --compare "question"` |
| Full copilot | `python scripts\copilot.py "question"` |
| Held-out eval | `python scripts\evaluate_retrieval.py --holdout --k 8` |
| Regression check | `python scripts\evaluate_retrieval.py --k 8 --compare-baseline` |
| Tests | `python -m pytest -q` |
| UI | `streamlit run app\streamlit_app.py` |
