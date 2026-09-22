"""
Database connectivity.

One place builds engines and connections, so the connection string is
constructed once and never logged. Callers receive a connection, never a URL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ..config import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


class DatabaseUnavailableError(RuntimeError):
    """Raised when the configured database cannot be reached."""


@contextmanager
def raw_connection(
    settings: Settings | None = None,
    *,
    database: str | None = None,
    autocommit: bool = False,
) -> Iterator[Any]:
    """Yield a DB-API connection for the selected backend.

    `autocommit=True` is required for DDL such as CREATE DATABASE, which cannot
    run inside an explicit transaction.
    """
    settings = settings or get_settings()
    if settings.database_backend == "postgresql":
        try:
            import psycopg

            conn = psycopg.connect(
                settings.postgres.connection_dsn(),
                connect_timeout=settings.postgres.connect_timeout,
                autocommit=autocommit,
            )
        except Exception as exc:
            raise DatabaseUnavailableError(
                "Cannot connect to PostgreSQL using POSTGRES_DSN. "
                f"Driver reported: {str(exc)[:200]}."
            ) from exc
    else:
        try:
            import pyodbc

            conn = pyodbc.connect(
                settings.database.odbc_connection_string(database=database),
                timeout=settings.database.connect_timeout,
                autocommit=autocommit,
            )
        except Exception as exc:
            # Deliberately reports the server and database but never the full
            # connection string, which may carry a password.
            raise DatabaseUnavailableError(
                f"Cannot connect to SQL Server at {settings.database.server!r} "
                f"(database {database or settings.database.database!r}). "
                f"Driver reported: {str(exc)[:200]}. "
                "Check that the SQL Server service is running and MSSQL_SERVER is correct."
            ) from exc

    try:
        yield conn
    finally:
        conn.close()


def create_engine(settings: Settings | None = None, **kwargs: Any) -> Engine:
    """Build a SQLAlchemy engine for pandas / ORM-style access."""
    from sqlalchemy import create_engine as _create_engine

    settings = settings or get_settings()
    if settings.database_backend == "postgresql":
        return _create_engine(
            settings.postgres.sqlalchemy_url(),
            pool_pre_ping=True,
            **kwargs,
        )
    return _create_engine(
        settings.database.sqlalchemy_url(),
        fast_executemany=True,  # bulk inserts for the synthetic data loader
        pool_pre_ping=True,
        **kwargs,
    )


def server_info(settings: Settings | None = None) -> dict[str, Any]:
    """Return edition, version and the security posture of the connection.

    `is_sysadmin` is surfaced because a sysadmin connection means the read-only
    database principal is not in force, and callers must be able to say so.
    """
    settings = settings or get_settings()
    with raw_connection(settings) as conn:
        cur = conn.cursor()
        if settings.database_backend == "postgresql":
            cur.execute(
                "SELECT version(), current_user, current_database(), "
                "       rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
            version, login, database, superuser, bypass_rls = cur.fetchone()
            return {
                "edition": "PostgreSQL",
                "version": version,
                "windows_auth_only": False,
                "login": login,
                "database": database,
                "is_sysadmin": bool(superuser or bypass_rls),
            }
        cur.execute(
            "SELECT CAST(SERVERPROPERTY('Edition') AS nvarchar(100)),"
            "       CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)),"
            "       CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS int),"
            "       SUSER_SNAME(), DB_NAME(),"
            "       IS_SRVROLEMEMBER('sysadmin')"
        )
        edition, version, integrated_only, login, database, sysadmin = cur.fetchone()
    return {
        "edition": edition,
        "version": version,
        "windows_auth_only": bool(integrated_only),
        "login": login,
        "database": database,
        "is_sysadmin": bool(sysadmin),
    }
