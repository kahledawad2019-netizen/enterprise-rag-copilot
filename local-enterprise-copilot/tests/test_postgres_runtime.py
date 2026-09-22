"""PostgreSQL adapter regressions that do not require a live server."""

from __future__ import annotations

from contextlib import contextmanager
from typing import ClassVar

from pydantic import SecretStr

from enterprise_copilot.config.settings import PostgresSettings
from enterprise_copilot.database import read_only_runner as runner_module
from enterprise_copilot.database.read_only_runner import ReadOnlyRunner
from enterprise_copilot.database.synthetic_loader import SyntheticLoader
from enterprise_copilot.security.sql_guard import SQLGuard
from enterprise_copilot.text_to_sql.schema_retriever import SQLContext, TableInfo


def _postgres_settings(settings):
    return settings.model_copy(
        update={
            "database_backend": "postgresql",
            "postgres": PostgresSettings(
                dsn=SecretStr("postgresql://copilot:secret@db.example.test/copilot")
            ),
        }
    )


def test_postgres_dsn_is_secret_and_sqlalchemy_uses_psycopg() -> None:
    postgres = PostgresSettings(
        dsn=SecretStr("postgresql://copilot:secret@db.example.test/copilot")
    )

    assert "secret" not in repr(postgres)
    assert postgres.sqlalchemy_url().startswith("postgresql+psycopg://")


def test_postgres_guard_emits_limit_not_top(settings) -> None:
    pg_settings = _postgres_settings(settings)
    guard = SQLGuard(pg_settings)

    result = guard.validate(
        "SELECT customer_id FROM core.customers WHERE tenant_id = 7",
        tenant_id=7,
    )

    assert result.is_safe
    assert result.effective_sql.endswith(f"LIMIT {settings.database.max_result_rows}")
    assert "TOP" not in result.effective_sql.upper()


class _Cursor:
    description: ClassVar[list[tuple[str]]] = [("customer_id",)]

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, *params: object):
        self.calls.append((sql, params))
        return self

    def fetchmany(self, _size: int) -> list[tuple[int]]:
        return [(7,)]


class _Connection:
    def __init__(self) -> None:
        self._cursor = _Cursor()

    def cursor(self) -> _Cursor:
        return self._cursor


def test_postgres_runner_sets_transaction_scoped_timeout_and_tenant(settings, monkeypatch) -> None:
    pg_settings = _postgres_settings(settings)
    connection = _Connection()

    @contextmanager
    def fake_connection(_settings):
        yield connection

    monkeypatch.setattr(runner_module, "raw_connection", fake_connection)

    rows, _ = ReadOnlyRunner(pg_settings)._execute(
        "SELECT customer_id FROM core.customers",
        tenant_id=7,
    )

    assert connection._cursor.calls[:2] == [
        ("SELECT set_config('statement_timeout', %s, true)", (("30000",),)),
        ("SELECT set_config('app.tenant_id', %s, true)", (("7",),)),
    ]
    assert connection._cursor.calls[2] == (
        "SELECT customer_id FROM core.customers",
        (),
    )
    assert rows == [{"customer_id": 7}]


def test_postgres_schema_context_uses_postgres_identifiers() -> None:
    context = SQLContext(
        question="customers",
        dialect="postgres",
        tables=[
            TableInfo(
                schema="core",
                name="customers",
                object_type="BASE TABLE",
                columns=[{"name": "customer_id", "type": "integer", "nullable": False}],
            )
        ],
    )

    rendered = context.render_schema()
    assert "core.customers" in rendered
    assert "[" not in rendered


def test_postgres_synthetic_loader_uses_psycopg_markers() -> None:
    class BulkCursor:
        def __init__(self) -> None:
            self.call = None

        def executemany(self, sql, rows) -> None:
            self.call = (sql, rows)

    class BulkConnection:
        def __init__(self) -> None:
            self.bulk_cursor = BulkCursor()

        def cursor(self):
            return self.bulk_cursor

    connection = BulkConnection()
    loader = SyntheticLoader(connection, dialect="postgres")

    loader._insert_many("core.customers", ["tenant_id", "display_name"], [(1, "Acme")])

    assert connection.bulk_cursor.call == (
        "INSERT INTO core.customers (tenant_id, display_name) VALUES (%s, %s)",
        [(1, "Acme")],
    )


def test_postgres_synthetic_loader_coerces_known_boolean_columns() -> None:
    class BulkCursor:
        def __init__(self) -> None:
            self.call = None

        def executemany(self, sql, rows) -> None:
            self.call = (sql, rows)

    class BulkConnection:
        def __init__(self) -> None:
            self.bulk_cursor = BulkCursor()

        def cursor(self):
            return self.bulk_cursor

    connection = BulkConnection()
    loader = SyntheticLoader(connection, dialect="postgres")

    loader._insert_many(
        "core.customers",
        ["tenant_id", "is_reactivated"],
        [(1, 0), (2, 1), (3, None)],
    )

    assert connection.bulk_cursor.call == (
        "INSERT INTO core.customers (tenant_id, is_reactivated) VALUES (%s, %s)",
        [(1, False), (2, True), (3, None)],
    )


def test_postgres_synthetic_loader_uses_copy_when_available() -> None:
    class CopySink:
        def __init__(self) -> None:
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def write_row(self, row) -> None:
            self.rows.append(row)

    class CopyCursor:
        def __init__(self) -> None:
            self.sql = None
            self.sink = CopySink()

        def copy(self, sql):
            self.sql = sql
            return self.sink

        def executemany(self, sql, rows) -> None:
            raise AssertionError("COPY-capable PostgreSQL cursor must not use executemany")

    class CopyConnection:
        def __init__(self) -> None:
            self.copy_cursor = CopyCursor()

        def cursor(self):
            return self.copy_cursor

    connection = CopyConnection()
    loader = SyntheticLoader(connection, dialect="postgres")

    loader._insert_many(
        "core.customers",
        ["tenant_id", "is_reactivated"],
        [(1, 0), (2, 1)],
    )

    assert connection.copy_cursor.sql == (
        "COPY core.customers (tenant_id, is_reactivated) FROM STDIN"
    )
    assert connection.copy_cursor.sink.rows == [(1, False), (2, True)]
