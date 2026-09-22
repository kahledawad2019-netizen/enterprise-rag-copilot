from __future__ import annotations

from contextlib import contextmanager
from typing import ClassVar

from enterprise_copilot.database import read_only_runner as runner_module
from enterprise_copilot.database.read_only_runner import ReadOnlyRunner


class _Cursor:
    description: ClassVar[list[tuple[str]]] = [("customer_id",)]

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchmany_size: int | None = None

    def execute(self, sql: str, *params: object) -> _Cursor:
        self.calls.append((sql, params))
        return self

    def fetchmany(self, size: int) -> list[tuple[int]]:
        self.fetchmany_size = size
        return [(1,), (2,), (3,)]

    def fetchall(self) -> list[tuple[int]]:
        raise AssertionError("bounded execution must not materialise the whole result")


class _Connection:
    def __init__(self) -> None:
        self.timeout = 0
        self._cursor = _Cursor()

    def cursor(self) -> _Cursor:
        return self._cursor


def test_execute_sets_rls_context_and_fetches_only_one_over_cap(settings, monkeypatch) -> None:
    connection = _Connection()

    @contextmanager
    def fake_connection(_settings):
        yield connection

    monkeypatch.setattr(runner_module, "raw_connection", fake_connection)
    monkeypatch.setattr(settings.database, "max_result_rows", 2)

    rows, columns = ReadOnlyRunner(settings)._execute("SELECT customer_id FROM x", tenant_id=7)

    assert connection._cursor.calls == [
        ("EXEC sys.sp_set_session_context @key=N'tenant_id', @value=?", (7,)),
        ("SELECT customer_id FROM x", ()),
    ]
    assert connection._cursor.fetchmany_size == 3
    assert rows == [{"customer_id": 1}, {"customer_id": 2}, {"customer_id": 3}]
    assert columns == ["customer_id"]


def test_execute_clears_pooled_connection_context_for_unscoped_call(settings, monkeypatch) -> None:
    connection = _Connection()

    @contextmanager
    def fake_connection(_settings):
        yield connection

    monkeypatch.setattr(runner_module, "raw_connection", fake_connection)

    ReadOnlyRunner(settings)._execute("SELECT 1", tenant_id=None)

    assert connection._cursor.calls[0] == (
        "EXEC sys.sp_set_session_context @key=N'tenant_id', @value=?",
        (None,),
    )
