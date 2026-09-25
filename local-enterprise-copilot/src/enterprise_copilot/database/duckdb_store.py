"""
Embedded analytical database: DuckDB.

Why a third backend
-------------------
SQL Server needs a Windows service and PostgreSQL needs a server or a Neon
account. Neither exists on a laptop without setup or on a free
Streamlit/Render-style host without a second provider. DuckDB is an in-process
file database with real schemas (`core.customers` works unchanged) and a SQL
dialect close to PostgreSQL, so the Text-to-SQL path can run everywhere with
nothing to provision.

How it stays one system rather than two
---------------------------------------
* The database is built from the **same PostgreSQL scripts** as Neon
  (`sql/postgres/`), translated mechanically below, and filled by the **same
  deterministic synthetic generator**. It is not a second schema to maintain.
* The model still writes **PostgreSQL**, and `SQLGuard` still validates
  PostgreSQL. Only at execution is the approved statement transpiled to DuckDB
  by sqlglot (`to_execution_sql`).
* Execution opens the file **read-only**. The guard allows only SELECT; the
  engine refusing every write is the independent second layer, the role that
  the read-only database login plays on SQL Server and PostgreSQL.

What is dropped in translation, and why that is acceptable
----------------------------------------------------------
* Foreign keys: DuckDB cannot declare them across schemas. The generator
  produces referentially consistent data (the PostgreSQL CI job enforces the
  constraints on the same generator output), and the relationships are kept
  in `ai.schema_relationships` so Text-to-SQL still learns the join paths.
* Roles, grants and row-level security: DuckDB has no users. Tenant isolation
  is enforced by the guard's injected tenant predicate, which is the primary
  control on every backend; RLS was the defence-in-depth layer.
* Indexes: unnecessary for a columnar engine at this size.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

log = logging.getLogger(__name__)

SCRIPTS = (
    "002_create_schemas.sql",
    "003_create_tables.sql",
    "005_create_views.sql",
    "007_seed_reference_data.sql",
)

_IDENTITY = re.compile(
    r"CREATE TABLE IF NOT EXISTS (\w+)\.(\w+) \(\s*\n\s*(\w+)\s+(INT|BIGINT)"
    r" GENERATED ALWAYS AS IDENTITY",
    re.IGNORECASE,
)
_FOREIGN_KEY = re.compile(
    r",(?:\s*--[^\n]*)?\s*CONSTRAINT\s+FK_\w+\s+FOREIGN KEY\s*\(\s*(\w+)\s*\)"
    r"\s*REFERENCES\s+(\w+)\.(\w+)\s*\(\s*(\w+)\s*\)",
    re.IGNORECASE,
)
_TABLE_HEADER = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\.(\w+)", re.IGNORECASE)
_DO_BLOCK = re.compile(r"DO \$\$.*?\$\$;", re.DOTALL)
# Case-sensitive and anchored to a statement start: comments in these
# scripts discuss grants in prose ("PostgreSQL grants CREATE ...").
_PRIVILEGES = re.compile(r"^[ \t]*(REVOKE|GRANT) [^;]*;", re.MULTILINE)
_VIEW_OPTIONS = re.compile(r"^WITH \(security_invoker = true\)\s*$", re.MULTILINE)

Relationship = tuple[str, str, str, str, str, str]


class DuckDBUnavailableError(RuntimeError):
    """The DuckDB file is missing or could not be built."""


def translate_postgres_script(text: str) -> tuple[str, list[Relationship]]:
    """Rewrite one sql/postgres script for DuckDB.

    Returns the script and the foreign keys it declared, as
    (schema, table, column, ref_schema, ref_table, ref_column).
    """
    relationships: list[Relationship] = []

    # Foreign keys: remember which table each belongs to, then remove.
    headers = [(m.start(), m.group(1), m.group(2)) for m in _TABLE_HEADER.finditer(text)]
    for match in _FOREIGN_KEY.finditer(text):
        owner = [h for h in headers if h[0] < match.start()]
        if owner:
            _, schema, table = owner[-1]
            column, ref_schema, ref_table, ref_column = match.groups()
            relationships.append((schema, table, column, ref_schema, ref_table, ref_column))
    text = _FOREIGN_KEY.sub("", text)

    # Identity columns become sequence defaults: the loader relies on the
    # database assigning keys, exactly as on SQL Server and PostgreSQL.
    def sequence(match: re.Match[str]) -> str:
        schema, table, column, kind = match.groups()
        name = f"{schema}.seq_{table}"
        return (
            f"CREATE SEQUENCE IF NOT EXISTS {name};\n"
            f"CREATE TABLE IF NOT EXISTS {schema}.{table} (\n"
            f"    {column} {kind} DEFAULT nextval('{name}')"
        )

    text = _IDENTITY.sub(sequence, text)
    text = _DO_BLOCK.sub("", text)
    text = _PRIVILEGES.sub("", text)
    text = _VIEW_OPTIONS.sub("", text)
    return text, relationships


def to_execution_sql(sql: str) -> str:
    """Transpile guard-approved PostgreSQL to DuckDB.

    The guard has already accepted `sql` as a single read-only SELECT;
    transpiling preserves the statement type, and the connection is read-only
    regardless, so a transpiler bug can produce a wrong answer but never a
    write.
    """
    import sqlglot

    statements = sqlglot.transpile(sql, read="postgres", write="duckdb")
    if len(statements) != 1:
        raise ValueError("expected exactly one statement after transpiling")
    return statements[0]


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


def _config(settings: Settings) -> dict[str, Any]:
    """Connection settings, passed as config rather than interpolated SQL.

    * memory and threads are bounded so one heavy query cannot take a small
      free-tier container down;
    * external access is off: no file reads (`read_text`, `read_csv`), no
      HTTP, no extension downloads. The guard already refuses those
      functions; this is the engine refusing them too, in case a spelling
      ever gets past the guard.
    """
    return {
        "memory_limit": str(settings.duckdb.memory_limit),
        "threads": int(settings.duckdb.threads),
        "enable_external_access": False,
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
    }


@contextmanager
def connect(settings: Settings, *, read_only: bool = True) -> Iterator[Any]:
    """Open the database. Read-only unless building it."""
    import duckdb

    path = settings.duckdb.path
    if read_only and not path.exists():
        raise DuckDBUnavailableError(
            f"The analytics database has not been built ({path.name} is missing). "
            "Run: python scripts/build_duckdb.py"
        )
    conn = duckdb.connect(str(path), read_only=read_only, config=_config(settings))
    try:
        if read_only:
            # After this no statement can change a setting - including
            # turning external access back on. Connections to the same file
            # share one database instance, so a concurrent one (a health
            # check during a query) finds it already locked; setting it again
            # would raise.
            locked = conn.execute("SELECT current_setting('lock_configuration')").fetchone()
            if not (locked and locked[0]):
                conn.execute("SET lock_configuration = true")
        yield conn
    finally:
        conn.close()


def execute_with_timeout(conn: Any, sql: str, timeout_seconds: float) -> Any:
    """Run `sql`, interrupting it after `timeout_seconds`.

    DuckDB has no statement_timeout setting; `interrupt()` from a timer thread
    is its documented way to cancel a running query.
    """
    timer = threading.Timer(timeout_seconds, conn.interrupt) if timeout_seconds > 0 else None
    if timer is not None:
        timer.daemon = True
        timer.start()
    try:
        return conn.execute(sql)
    finally:
        if timer is not None:
            timer.cancel()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def database_ready(settings: Settings) -> bool:
    path = settings.duckdb.path
    if not path.exists():
        return False
    try:
        with connect(settings) as conn:
            customers = conn.execute("SELECT count(*) FROM core.customers").fetchone()[0]
            return bool(customers)
    except Exception:
        return False


def build_database(settings: Settings, *, demo: bool | None = None) -> dict[str, int]:
    """Create the DuckDB file from the PostgreSQL scripts and synthetic data.

    Built into a temporary file and renamed into place, so a crash half way
    never leaves a partial database that looks valid.
    """
    import duckdb

    from .synthetic import SyntheticGenerator, Volumes
    from .synthetic_loader import SyntheticLoader, read_reference_data
    from .synthetic_operations import (
        generate_billing,
        generate_health,
        generate_incidents,
        generate_support,
        generate_usage,
    )

    target = settings.duckdb.path
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".building")
    for leftover in (temporary, temporary.with_suffix(".building.wal")):
        leftover.unlink(missing_ok=True)

    started = time.perf_counter()
    scripts_dir = settings.project_root / "sql" / "postgres"
    conn = duckdb.connect(str(temporary), config=_config(settings))
    try:
        relationships: list[Relationship] = []
        for name in SCRIPTS:
            script, found = translate_postgres_script(
                (scripts_dir / name).read_text(encoding="utf-8")
            )
            relationships.extend(found)
            conn.execute(script)

        conn.execute(
            "CREATE TABLE ai.schema_relationships ("
            " table_schema VARCHAR, table_name VARCHAR, column_name VARCHAR,"
            " ref_schema VARCHAR, ref_table VARCHAR, ref_column VARCHAR)"
        )
        if relationships:
            conn.executemany(
                "INSERT INTO ai.schema_relationships VALUES (?, ?, ?, ?, ?, ?)", relationships
            )

        use_demo = settings.demo_mode if demo is None else demo
        volumes = Volumes.demo() if use_demo else Volumes()
        generator = SyntheticGenerator(settings, volumes, read_reference_data(conn))
        generator.generate_customers()
        generator.generate_subscriptions()
        generate_usage(generator)
        generate_billing(generator)
        generate_incidents(generator)
        generate_support(generator)
        generate_health(generator)

        SyntheticLoader(conn, dialect="duckdb").load(generator.data)
        conn.execute("CHECKPOINT")
    finally:
        conn.close()

    os.replace(temporary, target)
    counts = generator.data.counts()
    log.info(
        "DuckDB analytics database built at %s in %.1fs (%d customers)",
        target.name,
        time.perf_counter() - started,
        counts.get("customers", 0),
    )
    return counts


def ensure_database(settings: Settings) -> bool:
    """Build the database if it is missing. Returns True if it was built."""
    if database_ready(settings):
        return False
    build_database(settings)
    return True


def read_relationships(conn: Any) -> list[Relationship]:
    try:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT table_schema, table_name, column_name, ref_schema, ref_table, ref_column "
                "FROM ai.schema_relationships"
            ).fetchall()
        ]
    except Exception:
        return []


def default_path(project_root: Path) -> Path:
    return project_root / "data" / "warehouse" / "northwind.duckdb"


__all__ = [
    "DuckDBUnavailableError",
    "build_database",
    "connect",
    "database_ready",
    "default_path",
    "ensure_database",
    "execute_with_timeout",
    "read_relationships",
    "to_execution_sql",
    "translate_postgres_script",
]
