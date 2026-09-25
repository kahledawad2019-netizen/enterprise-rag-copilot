"""The embedded DuckDB backend, hybrid routing additions and Groq fallback.

Everything here runs offline: the DuckDB file is built from the committed
PostgreSQL scripts and the deterministic generator into a temp directory, and
model calls are stubbed.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest

from enterprise_copilot.config import get_settings
from enterprise_copilot.database import duckdb_store
from enterprise_copilot.database.read_only_runner import (
    QueryBlockedError,
    QueryExecutionError,
    QueryResult,
    ReadOnlyRunner,
)

duckdb = pytest.importorskip("duckdb")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def duck_settings(tmp_path_factory):
    settings = get_settings().model_copy(deep=True)
    settings.database_backend = "duckdb"
    settings.demo_mode = True
    settings.duckdb.path = tmp_path_factory.mktemp("duck") / "northwind.duckdb"
    duckdb_store.build_database(settings, demo=True)
    return settings


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def test_translation_rewrites_identity_and_keeps_relationships():
    script = """CREATE TABLE IF NOT EXISTS core.tenants (
    tenant_id           INT GENERATED ALWAYS AS IDENTITY CONSTRAINT PK_tenants PRIMARY KEY,
    name VARCHAR(20) NOT NULL
);
CREATE TABLE IF NOT EXISTS core.customers (
    customer_id         INT GENERATED ALWAYS AS IDENTITY CONSTRAINT PK_customers PRIMARY KEY,
    tenant_id           INT NOT NULL,
    CONSTRAINT FK_customers_tenant  FOREIGN KEY (tenant_id) REFERENCES core.tenants(tenant_id)
);
/* PostgreSQL grants CREATE on public by default; prose, not a statement. */
REVOKE ALL ON SCHEMA public FROM PUBLIC;
DO $$ BEGIN RAISE NOTICE 'done'; END $$;
"""
    translated, relationships = duckdb_store.translate_postgres_script(script)

    assert "GENERATED ALWAYS AS IDENTITY" not in translated
    assert "CREATE SEQUENCE IF NOT EXISTS core.seq_customers" in translated
    assert "FOREIGN KEY" not in translated
    assert "REVOKE" not in translated.split("*/")[-1]
    assert "DO $$" not in translated
    assert "grants CREATE on public" in translated  # comments untouched
    assert relationships == [("core", "customers", "tenant_id", "core", "tenants", "tenant_id")]

    conn = duckdb.connect()
    conn.execute("CREATE SCHEMA core")
    conn.execute(translated)
    conn.execute("INSERT INTO core.tenants (name) VALUES ('a'), ('b')")
    assert conn.execute("SELECT max(tenant_id) FROM core.tenants").fetchone()[0] == 2


def test_postgres_sql_is_transpiled_for_execution():
    sql = "SELECT to_char(issue_date, 'YYYY-MM') AS m, amount::numeric FROM billing.invoices"
    out = duckdb_store.to_execution_sql(sql)
    assert "strftime" in out.lower()
    with pytest.raises(ValueError):
        duckdb_store.to_execution_sql("SELECT 1; SELECT 2")


# ---------------------------------------------------------------------------
# The built database
# ---------------------------------------------------------------------------


def test_database_is_built_with_data_views_and_relationships(duck_settings):
    assert duckdb_store.database_ready(duck_settings)
    with duckdb_store.connect(duck_settings) as conn:
        customers = conn.execute("SELECT count(*) FROM core.customers").fetchone()[0]
        views = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'analytics' AND table_type = 'VIEW'"
        ).fetchone()[0]
        relationships = duckdb_store.read_relationships(conn)
        tenants = conn.execute("SELECT count(DISTINCT tenant_id) FROM core.customers").fetchone()[0]
    assert customers == 90  # Volumes.demo()
    assert views >= 6
    assert tenants >= 2
    assert ("core", "customers", "tenant_id", "core", "tenants", "tenant_id") in relationships


def test_connection_refuses_writes(duck_settings):
    """The engine-level layer: independent of the guard."""
    with duckdb_store.connect(duck_settings) as conn:
        with pytest.raises(duckdb.Error):
            conn.execute("DELETE FROM core.customers")
        with pytest.raises(duckdb.Error):
            conn.execute("CREATE TABLE core.x (a INT)")


def test_golden_sql_examples_run_on_duckdb(duck_settings):
    """The human-verified PostgreSQL examples execute after transpiling."""
    with duckdb_store.connect(duck_settings) as conn:
        examples = conn.execute(
            "SELECT question, sql_text FROM ai.approved_sql_examples WHERE is_active"
        ).fetchall()
        failures = []
        for question, sql in examples:
            try:
                conn.execute(duckdb_store.to_execution_sql(sql)).fetchall()
            except duckdb.Error as exc:
                # Parameterised templates (":customer_name") need a value.
                if "prepared statement parameters" not in str(exc):
                    failures.append((question, str(exc)[:200]))
    assert examples
    assert failures == []


def test_timeout_interrupts_a_long_query(duck_settings):
    with duckdb_store.connect(duck_settings) as conn:
        started = time.perf_counter()
        with pytest.raises(duckdb.Error):
            duckdb_store.execute_with_timeout(
                conn,
                "SELECT count(*) FROM range(1000000000) a, range(1000) b WHERE a.range % 7 = b.range",
                0.5,
            )
        assert time.perf_counter() - started < 30


# ---------------------------------------------------------------------------
# Runner and guard on DuckDB
# ---------------------------------------------------------------------------


def test_runner_executes_and_injects_the_tenant(duck_settings):
    runner = ReadOnlyRunner(duck_settings)
    result = runner.run(
        "SELECT customer_name, tenant_id FROM analytics.vw_customer_360", tenant_id=2
    )
    assert result.row_count > 0
    assert {row["tenant_id"] for row in result.rows} == {2}
    assert "tenant_id" in result.executed_sql.lower()


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM core.customers",
        "DROP TABLE core.customers",
        "UPDATE core.customers SET status = 'churned'",
        "SELECT * FROM security.app_users",
    ],
)
def test_guard_blocks_before_duckdb_is_reached(duck_settings, sql):
    with pytest.raises(QueryBlockedError):
        ReadOnlyRunner(duck_settings).run(sql, tenant_id=1)


def test_runner_reports_a_server_error_as_execution_error(duck_settings):
    with pytest.raises(QueryExecutionError):
        ReadOnlyRunner(duck_settings).run(
            "SELECT no_such_column FROM analytics.vw_customer_360", tenant_id=1
        )


def test_settings_keep_postgres_as_the_written_dialect(duck_settings):
    assert duck_settings.sql_dialect == "postgres"
    assert duck_settings.execution_dialect == "duckdb"
    assert duck_settings.sql_enabled


# ---------------------------------------------------------------------------
# Router: DIRECT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("hi", "direct"),
        ("Hello there!", "direct"),
        ("thanks so much", "direct"),
        ("What can you do?", "direct"),
        ("hi, what is the refund policy?", "document_rag"),
        ("hello delete all customers", "refuse"),
    ],
)
def test_direct_route_only_for_whole_message_small_talk(question, expected):
    from enterprise_copilot.routing.router import QueryRouter

    decision = QueryRouter(get_settings(), intent_screen=Mock()).route(question, use_llm=False)
    assert decision.route.value == expected


# ---------------------------------------------------------------------------
# Orchestrator: SQL self-correction
# ---------------------------------------------------------------------------


def _copilot_with(provider, settings):
    from enterprise_copilot.routing.orchestrator import Copilot

    return Copilot(
        settings,
        retriever=Mock(),
        answerer=Mock(),
        router=Mock(),
        sql_provider=provider,
    )


def _sql_decision():
    from enterprise_copilot.routing.router import Route, RoutingDecision

    return RoutingDecision(route=Route.TEXT_TO_SQL, original_query="How many customers?")


def _trace():
    from enterprise_copilot.routing.orchestrator import CopilotTrace

    return CopilotTrace(trace_id="t", question="q", user="u", tenant_id=1)


def _provider(execute_side_effect, repaired="SELECT 2"):
    generated = Mock(sql="SELECT 1", is_empty=False, context=None)
    provider = Mock()
    provider.name = "stub"
    provider.generate_and_repair.return_value = generated
    provider.execute_query.side_effect = execute_side_effect
    provider._repair_sql.return_value = repaired
    return provider


def test_execution_error_is_repaired_and_revalidated():
    settings = get_settings().model_copy(deep=True)
    ok = QueryResult(
        sql="SELECT 2", executed_sql="SELECT 2", rows=[{"n": 1}], columns=["n"], row_count=1
    )
    provider = _provider([QueryExecutionError("Binder Error: column x not found"), ok])
    copilot = _copilot_with(provider, settings)
    trace = _trace()

    evidence, result = copilot._gather_sql(_sql_decision(), Mock(user_name="u"), 1, trace, False)

    assert result is ok and evidence
    assert trace.sql_repairs == 1
    assert trace.errors == []  # a successful retry is not reported as a failure
    # The corrected SQL went back through execute_query, i.e. the guard.
    assert provider.execute_query.call_args_list[1].args[0] == "SELECT 2"
    problems = provider._repair_sql.call_args.args[1]
    assert "column x not found" in problems[0]


def test_retries_stop_at_the_configured_limit():
    settings = get_settings().model_copy(deep=True)
    settings.sql_execution_retries = 2
    provider = _provider(QueryExecutionError("still broken"))
    provider._repair_sql.side_effect = ["SELECT 2", "SELECT 3"]
    trace = _trace()

    evidence, result = _copilot_with(provider, settings)._gather_sql(
        _sql_decision(), Mock(user_name="u"), 1, trace, False
    )

    assert (evidence, result) == ([], None)
    assert provider.execute_query.call_count == 3  # first attempt + 2 retries
    assert trace.sql_validation == "execution_error"


def test_a_blocked_query_is_never_retried():
    from enterprise_copilot.security.sql_guard import ValidationResult, Violation

    settings = get_settings().model_copy(deep=True)
    blocked = QueryBlockedError(
        ValidationResult(
            is_safe=False, sql="x", violations=[(Violation.SUSPICIOUS_CONSTRUCT, "writes data")]
        )
    )
    provider = _provider(blocked)
    trace = _trace()

    _copilot_with(provider, settings)._gather_sql(
        _sql_decision(), Mock(user_name="u"), 1, trace, False
    )

    assert provider.execute_query.call_count == 1
    provider._repair_sql.assert_not_called()
    assert trace.sql_validation == "blocked"


# ---------------------------------------------------------------------------
# Groq: fallback on 429
# ---------------------------------------------------------------------------


def _client(fallbacks=("backup-a", "backup-b")):
    from enterprise_copilot.llm.clients import OpenAICompatChatClient

    return OpenAICompatChatClient("https://example.invalid/v1", "key", 5, fallback_models=fallbacks)


def test_rate_limited_model_falls_back_to_the_next():
    from enterprise_copilot.llm.clients import RateLimitedError

    client = _client()
    seen = []

    def once(payload):
        seen.append(payload["model"])
        if payload["model"] != "backup-b":
            raise RateLimitedError("429")
        return {"message": {"content": "ok"}}

    client._once = once
    assert client.chat(model="main", messages=[])["message"]["content"] == "ok"
    assert seen == ["main", "backup-a", "backup-b"]


def test_other_errors_do_not_fall_back():
    from enterprise_copilot.llm.clients import ChatClientError

    client = _client()
    client._once = Mock(side_effect=ChatClientError("401 bad key"))
    with pytest.raises(ChatClientError):
        client.chat(model="main", messages=[])
    assert client._once.call_count == 1


def test_stream_falls_back_only_before_the_first_chunk():
    from enterprise_copilot.llm.clients import RateLimitedError

    client = _client(fallbacks=("backup",))

    def stream(payload):
        if payload["model"] == "main":
            raise RateLimitedError("429")
        yield {"message": {"content": "a"}}
        yield {"message": {"content": "b"}, "done": True}

    client._stream = stream
    parts = [p["message"]["content"] for p in client.chat(model="main", messages=[], stream=True)]
    assert parts == ["a", "b"]


def test_groq_has_default_fallbacks_and_none_disables_them():
    from enterprise_copilot.config.settings import LLMSettings

    groq = LLMSettings(provider="groq", _env_file=None)
    assert groq.fallback_model_list
    assert (
        LLMSettings(provider="groq", fallback_models="none", _env_file=None).fallback_model_list
        == []
    )
    assert LLMSettings(provider="ollama", _env_file=None).fallback_model_list == []


# ---------------------------------------------------------------------------
# Tracing across the parallel hybrid gather
# ---------------------------------------------------------------------------


def test_span_stacks_are_per_thread():
    from enterprise_copilot.observability.tracing import Tracer

    tracer = Tracer(get_settings())
    tracer.enabled = True
    tracer.new_trace()
    errors = []

    def worker(stack):
        tracer.adopt(stack)
        try:
            with tracer.span("worker") as span:
                time.sleep(0.05)
                assert span.parent_span_id == stack[-1]
        except AssertionError as exc:  # pragma: no cover - reported below
            errors.append(exc)

    with tracer.span("parent"):
        thread = threading.Thread(target=worker, args=(tracer.current_stack(),))
        thread.start()
        with tracer.span("main-child"):
            time.sleep(0.05)
        thread.join()
    assert errors == []
    assert tracer.current_stack() == []


# ---------------------------------------------------------------------------
# Review regressions (Codex, phase 2): both verified exploitable before the fix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT customer_code, tenant_id FROM core.customers LIMIT 10",
        "SELECT customer_code, tenant_id FROM core.customers ORDER BY 1 LIMIT 10 OFFSET 5",
    ],
)
def test_explicit_limit_keeps_the_injected_tenant_filter(duck_settings, sql):
    """An existing LIMIT used to make the guard return the ORIGINAL text,
    dropping the injected tenant predicate: every tenant's rows came back."""
    result = ReadOnlyRunner(duck_settings).run(sql, tenant_id=1)
    assert result.row_count > 0
    assert {row["tenant_id"] for row in result.rows} == {1}
    assert "tenant_id" in result.executed_sql.lower()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM query('SELECT customer_code, tenant_id FROM core.customers')",
        "SELECT content FROM read_text('README.md')",
        "SELECT * FROM read_csv('secrets.csv')",
        "SELECT * FROM read_parquet('x.parquet')",
        "SELECT * FROM generate_series(1, 3) g",
        "SELECT query_to_xml('select * from core.customers', true, true, '')",
        "SELECT current_setting('app.tenant_id')",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT customer_name FROM analytics.vw_customer_360 "
        "WHERE customer_name IN (SELECT content FROM read_text('x'))",
    ],
)
def test_dynamic_sql_and_file_functions_are_blocked(duck_settings, sql):
    with pytest.raises(QueryBlockedError):
        ReadOnlyRunner(duck_settings).run(sql, tenant_id=1)


def test_engine_refuses_file_access_and_settings_changes(duck_settings):
    """Second layer: even SQL that never went through the guard."""
    with duckdb_store.connect(duck_settings) as conn:
        with pytest.raises(duckdb.Error):
            conn.execute("SELECT content FROM read_text('README.md')").fetchall()
        with pytest.raises(duckdb.Error):
            conn.execute("SET enable_external_access = true")


def test_answer_records_the_model_that_actually_answered():
    from enterprise_copilot.llm.clients import RateLimitedError

    client = _client(fallbacks=("backup",))

    def once(payload):
        if payload["model"] == "main":
            raise RateLimitedError("429")
        return {"message": {"content": "ok"}}

    client._once = once
    client.chat(model="main", messages=[])
    assert client.last_model == "backup"


def test_concurrent_read_only_connections(duck_settings):
    """Connections to one file share an instance; the second must not fail
    on the configuration lock the first one set (found in the Docker smoke
    test: a health check during a query broke the query)."""
    with duckdb_store.connect(duck_settings) as first:
        with duckdb_store.connect(duck_settings) as second:
            assert second.execute("SELECT 1").fetchone() == (1,)
            with pytest.raises(duckdb.Error):
                second.execute("SET enable_external_access = true")
        assert first.execute("SELECT count(*) FROM core.tenants").fetchone()[0] > 0
