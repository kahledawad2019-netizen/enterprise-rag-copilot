r"""
Pre-flight check. Verifies every moving part before you start building.

Run:  .venv\Scripts\python scripts\check_env.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"{status} {name}" + (f"  -  {detail}" if detail else ""))


def check_python() -> None:
    v = sys.version_info
    record(OK if v >= (3, 10) else BAD, "Python", f"{v.major}.{v.minor}.{v.micro}")


def check_packages() -> None:
    import importlib.metadata as md

    for pkg in ("vanna", "chromadb", "ollama", "pyodbc", "sqlalchemy",
                "pandas", "plotly", "streamlit", "pydantic-settings"):
        try:
            record(OK, f"package {pkg}", md.version(pkg))
        except Exception:
            record(BAD, f"package {pkg}", "not installed")


def check_odbc_driver(settings) -> None:
    import pyodbc

    drivers = pyodbc.drivers()
    if settings.mssql_driver in drivers:
        record(OK, "ODBC driver", settings.mssql_driver)
    else:
        record(BAD, "ODBC driver",
               f"{settings.mssql_driver!r} not found. Available: {drivers}")


def check_sql_server(settings) -> None:
    import pyodbc

    try:
        with pyodbc.connect(settings.odbc_connection_string(), timeout=10) as conn:
            cur = conn.cursor()
            cur.execute("SELECT @@VERSION, DB_NAME(), SUSER_SNAME()")
            version, db, login = cur.fetchone()
            record(OK, "SQL Server", f"{version.splitlines()[0].strip()}")
            record(OK, "  database / login", f"{db} as {login}")

            cur.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_TYPE = 'BASE TABLE'
            """)
            record(OK, "  tables visible", str(cur.fetchone()[0]))

            cur.execute("SELECT IS_MEMBER('db_owner'), IS_MEMBER('db_datawriter')")
            owner, writer = cur.fetchone()
            if owner or writer:
                record(WARN, "  privileges",
                       "this login can WRITE. Use a db_datareader-only login "
                       "(see README).")
            else:
                record(OK, "  privileges", "read-only login")
    except Exception as exc:
        record(BAD, "SQL Server", f"{type(exc).__name__}: {str(exc)[:200]}")


def check_ollama(settings) -> None:
    try:
        import ollama

        client = ollama.Client(settings.ollama_host)
        models = [m["model"] for m in client.list().get("models", [])]
        record(OK, "Ollama service", f"{settings.ollama_host} ({len(models)} models)")
        if settings.ollama_model in models:
            record(OK, "  model", settings.ollama_model)
        else:
            record(BAD, "  model",
                   f"{settings.ollama_model} missing. Run: ollama pull {settings.ollama_model}")
            return
        reply = client.chat(
            model=settings.ollama_model,
            messages=[{"role": "user", "content": "Reply with only: PONG"}],
        )
        record(OK, "  generation", reply["message"]["content"].strip()[:40])
    except Exception as exc:
        record(BAD, "Ollama", f"{type(exc).__name__}: {str(exc)[:200]}")


def check_vector_store(settings) -> None:
    try:
        from chromadb.utils import embedding_functions

        vec = embedding_functions.DefaultEmbeddingFunction()(["ping"])
        record(OK, "Embeddings", f"local ONNX MiniLM, dim={len(vec[0])}")
        record(OK, "Chroma path", str(settings.chroma_path))
    except Exception as exc:
        record(BAD, "Embeddings", f"{type(exc).__name__}: {str(exc)[:200]}")


def check_secrets_hygiene(settings) -> None:
    root = Path(__file__).resolve().parent.parent
    gitignore = root / ".gitignore"
    if gitignore.exists() and ".env" in gitignore.read_text(encoding="utf-8"):
        record(OK, ".gitignore", ".env is excluded from git")
    else:
        record(BAD, ".gitignore", ".env is NOT excluded - secrets could be committed")

    if (root / ".env").exists():
        record(OK, ".env file", "present")
    else:
        record(WARN, ".env file", "missing - copy .env.example to .env")

    if settings.mssql_auth_mode == "windows":
        record(OK, "DB credentials", "Windows auth - no password stored on disk")
    else:
        record(WARN, "DB credentials",
               "SQL auth - keep MSSQL_PASSWORD out of git, or move it to the "
               "Windows Credential Manager (scripts/set_secret.py)")

    if settings.mssql_encrypt:
        record(OK, "TLS", "Encrypt=yes")
    else:
        record(BAD, "TLS", "Encrypt=no - the connection is not encrypted")

    if settings.mssql_trust_server_certificate:
        record(WARN, "Certificate validation",
               "TrustServerCertificate=yes - acceptable for a local instance "
               "only; set false against a remote server")
    else:
        record(OK, "Certificate validation", "server certificate is verified")

    if settings.read_only:
        record(OK, "SQL guardrails", f"read-only, max {settings.max_rows} rows")
    else:
        record(WARN, "SQL guardrails", "READ_ONLY=false - the agent may write!")


def check_guardrails() -> None:
    from src.security import is_read_only

    cases = [
        ("SELECT TOP 10 * FROM Sales", True),
        ("WITH c AS (SELECT 1 AS x) SELECT * FROM c", True),
        ("DROP TABLE Sales", False),
        ("SELECT 1; DROP TABLE Sales", False),
        ("SELECT 1 /* hide */ ; DELETE FROM Sales", False),
        ("UPDATE Sales SET total = 0", False),
        ("SELECT * INTO Copy FROM Sales", False),
        ("EXEC sp_who", False),
    ]
    failed = [sql for sql, expected in cases if is_read_only(sql) is not expected]
    if failed:
        record(BAD, "Guardrail self-test", f"{len(failed)} case(s) wrong: {failed}")
    else:
        record(OK, "Guardrail self-test", f"{len(cases)}/{len(cases)} cases correct")


def main() -> int:
    print("=" * 72)
    print("RAG SYSTEM - environment check")
    print("=" * 72)

    check_python()
    check_packages()
    check_guardrails()

    try:
        from src.settings import get_settings

        settings = get_settings()
    except Exception as exc:
        record(BAD, "Configuration", f"{type(exc).__name__}: {exc}")
        _summary()
        return 1

    check_odbc_driver(settings)
    check_sql_server(settings)
    check_ollama(settings)
    check_vector_store(settings)
    check_secrets_hygiene(settings)
    return _summary()


def _summary() -> int:
    fails = sum(1 for s, _, _ in results if s == BAD)
    warns = sum(1 for s, _, _ in results if s == WARN)
    print("=" * 72)
    print(f"{len(results)} checks | {fails} failed | {warns} warnings")
    print("=" * 72)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
