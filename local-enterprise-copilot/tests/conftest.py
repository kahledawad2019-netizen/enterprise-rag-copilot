"""Shared pytest fixtures.

Integration tests are skipped rather than failed when their service is absent,
so `pytest` is useful on a machine with no SQL Server and no Ollama. Each skip
states what is missing, so a skipped test is never mistaken for a passing one.
"""

from __future__ import annotations

import pytest

from enterprise_copilot.config import Settings, get_settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def sql_server_available(settings: Settings) -> bool:
    try:
        from enterprise_copilot.database.connection import raw_connection

        with raw_connection(settings) as conn:
            conn.cursor().execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def ollama_available(settings: Settings) -> bool:
    try:
        import ollama

        ollama.Client(settings.ollama.host).list()
        return True
    except Exception:
        return False


@pytest.fixture
def require_sql_server(sql_server_available: bool) -> None:
    if not sql_server_available:
        pytest.skip("SQL Server is not reachable; check MSSQL_SERVER in .env")


@pytest.fixture
def require_ollama(ollama_available: bool) -> None:
    if not ollama_available:
        pytest.skip("Ollama is not running; start it and pull the profile models")


@pytest.fixture(scope="session")
def reference_data(settings: Settings, sql_server_available: bool) -> dict:
    """Reference rows the synthetic generator needs."""
    if not sql_server_available:
        pytest.skip("SQL Server is not reachable")

    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from generate_synthetic_data import load_reference_data

    from enterprise_copilot.database.connection import raw_connection

    with raw_connection(settings) as conn:
        return load_reference_data(conn)
