"""
Text-to-SQL provider tests.

Two things are being protected here:

1. **The provider interface holds.** Vanna and the native provider must be
   interchangeable, and neither may validate or execute its own SQL.
2. **The Vanna bracket defect stays fixed.** ADR-002 records that Vanna's
   `extract_sql` truncates at the first `[`, destroying ordinary T-SQL. The
   override is covered here so an upstream change cannot silently reintroduce
   it.
"""

from __future__ import annotations

import pytest

from enterprise_copilot.text_to_sql.native import extract_sql
from enterprise_copilot.text_to_sql.provider import SQLRequest, TextToSQLProvider


class TestSQLExtraction:
    """The defect in Vanna's own extractor, asserted fixed."""

    def test_bracketed_identifiers_survive(self) -> None:
        """Vanna's version cuts at the first '[' and returns 'SELECT '."""
        response = (
            "SELECT [State], COUNT([CustomerID]) AS [CustomerCount]\n"
            "FROM [Customers]\nGROUP BY [State]"
        )
        extracted = extract_sql(response)
        assert extracted.startswith("SELECT [State]")
        assert "COUNT([CustomerID])" in extracted
        assert "GROUP BY [State]" in extracted

    def test_schema_qualified_brackets_survive(self) -> None:
        response = "SELECT TOP (5) [customer_name] FROM [analytics].[vw_customer_360]"
        assert "[analytics].[vw_customer_360]" in extract_sql(response)

    def test_fenced_block_is_unwrapped(self) -> None:
        response = "Here you go:\n```sql\nSELECT TOP (5) [name] FROM [core].[products];\n```\nHope that helps."
        extracted = extract_sql(response)
        assert extracted == "SELECT TOP (5) [name] FROM [core].[products]"

    def test_cte_is_preserved(self) -> None:
        response = "WITH c AS (SELECT [x] FROM [t]) SELECT * FROM c;"
        assert extract_sql(response).startswith("WITH c AS")

    def test_trailing_prose_is_dropped(self) -> None:
        response = "SELECT [a] FROM [b]\n\nThis query returns all rows."
        assert extract_sql(response) == "SELECT [a] FROM [b]"

    def test_semicolon_terminates_the_statement(self) -> None:
        assert extract_sql("SELECT 1; DROP TABLE x") == "SELECT 1"

    def test_empty_and_non_sql_responses(self) -> None:
        assert extract_sql("") == ""
        assert extract_sql("I cannot answer that question.") == ""


class TestProviderInterface:
    def test_both_providers_implement_the_interface(self) -> None:
        from enterprise_copilot.text_to_sql.native import NativeTextToSQLProvider

        required = ("generate_query", "validate_query", "execute_query",
                    "explain_result", "get_trace_metadata")
        for method in required:
            assert hasattr(NativeTextToSQLProvider, method), f"native missing {method}"

        try:
            from enterprise_copilot.text_to_sql.vanna_provider import VannaTextToSQLProvider
        except ImportError:
            pytest.skip("vanna not installed")
        for method in required:
            assert hasattr(VannaTextToSQLProvider, method), f"vanna missing {method}"

    def test_validation_and_execution_are_owned_by_the_base_class(self) -> None:
        """A provider that could validate its own SQL could approve its own SQL."""
        from enterprise_copilot.text_to_sql.native import NativeTextToSQLProvider

        assert NativeTextToSQLProvider.validate_query is TextToSQLProvider.validate_query
        assert NativeTextToSQLProvider.execute_query is TextToSQLProvider.execute_query

        try:
            from enterprise_copilot.text_to_sql.vanna_provider import VannaTextToSQLProvider
        except ImportError:
            return
        assert VannaTextToSQLProvider.validate_query is TextToSQLProvider.validate_query
        assert VannaTextToSQLProvider.execute_query is TextToSQLProvider.execute_query

    def test_vanna_version_is_read_from_metadata_not_dunder(self) -> None:
        """`vanna.__version__` reports 0.1.0 while the distribution is 2.0.2."""
        try:
            import vanna

            from enterprise_copilot.text_to_sql.vanna_provider import installed_vanna_version
        except ImportError:
            pytest.skip("vanna not installed")

        reported = installed_vanna_version()
        assert reported.startswith("2."), f"expected a 2.x distribution, got {reported}"
        assert vanna.__version__ != reported, (
            "vanna.__version__ now agrees with the distribution version; "
            "ADR-002 can be simplified"
        )


@pytest.mark.integration
@pytest.mark.requires_sqlserver
class TestSchemaRetrieval:
    @pytest.fixture(scope="class")
    def retriever(self, request):
        from enterprise_copilot.text_to_sql.schema_retriever import SchemaRetriever

        try:
            instance = SchemaRetriever()
            instance.load_catalog()
        except Exception as exc:
            pytest.skip(f"database unavailable: {exc}")
        return instance

    def test_catalog_excludes_ai_and_security_schemas(self, retriever) -> None:
        """The model must never even be told these schemas exist."""
        schemas = {t.schema for t in retriever.load_catalog()}
        assert "ai" not in schemas
        assert "security" not in schemas

    def test_catalog_contains_the_analytics_views(self, retriever) -> None:
        names = {t.qualified for t in retriever.load_catalog()}
        assert "analytics.vw_customer_360" in names
        assert "analytics.vw_sla_performance" in names

    def test_selection_returns_a_subset_not_everything(self, retriever) -> None:
        """Sending the whole schema on every question is what this avoids."""
        selected = retriever.select_tables("Which customers have the highest ARR?", limit=6)
        assert 0 < len(selected) <= 9
        assert len(selected) < len(retriever.load_catalog())

    def test_glossary_matches_the_question(self, retriever) -> None:
        terms = {g["term"] for g in retriever.select_glossary("Calculate MRR")}
        assert "MRR" in terms

    def test_superseded_glossary_entries_are_never_returned(self, retriever) -> None:
        """MRR v1.0 wrongly included trials; feeding it back would undo the fix.

        Version number alone is not the test: several terms are legitimately at
        v1.0 because they have never been revised. What must never appear is a
        row marked `is_current = 0`, and for MRR specifically that means v2.0.
        """
        returned = retriever.select_glossary("Calculate MRR", limit=10)
        mrr = next((t for t in returned if t["term"] == "MRR"), None)
        assert mrr is not None, "MRR was not retrieved for an MRR question"
        assert mrr["version"] == "2.0", (
            f"retrieved the superseded MRR definition (v{mrr['version']}), "
            "which wrongly includes trials"
        )

        # Cross-check against the database: every returned term must be current.
        from enterprise_copilot.database.connection import raw_connection

        with raw_connection() as conn:
            cursor = conn.cursor()
            for term in returned:
                cursor.execute(
                    "SELECT is_current FROM ai.business_glossary WHERE term = ? AND version = ?",
                    term["term"], term["version"],
                )
                row = cursor.fetchone()
                assert row is not None and row[0] == 1, (
                    f"{term['term']} v{term['version']} is not the current definition"
                )

    def test_approved_examples_are_retrieved(self, retriever) -> None:
        examples = retriever.select_examples("Which five customers have the highest ARR?")
        assert examples
        assert all("sql_text" in e for e in examples)


@pytest.mark.integration
@pytest.mark.requires_sqlserver
class TestReadOnlyRunner:
    @pytest.fixture(scope="class")
    def runner(self):
        from enterprise_copilot.database.read_only_runner import ReadOnlyRunner

        try:
            instance = ReadOnlyRunner()
            instance.run(
                "SELECT TOP 1 customer_id FROM analytics.vw_customer_360 WHERE tenant_id = 1",
                tenant_id=1,
            )
        except Exception as exc:
            pytest.skip(f"database unavailable: {exc}")
        return instance

    def test_executes_a_safe_query(self, runner) -> None:
        result = runner.run(
            "SELECT TOP 3 customer_name FROM analytics.vw_customer_360 WHERE tenant_id = 1",
            tenant_id=1,
        )
        assert result.row_count == 3
        assert "customer_name" in result.columns

    def test_blocks_a_write(self, runner) -> None:
        from enterprise_copilot.database.read_only_runner import QueryBlockedError

        with pytest.raises(QueryBlockedError):
            runner.run("DELETE FROM core.customers", tenant_id=1)

    def test_blocks_a_cross_tenant_read(self, runner) -> None:
        from enterprise_copilot.database.read_only_runner import QueryBlockedError

        with pytest.raises(QueryBlockedError):
            runner.run(
                "SELECT customer_name FROM analytics.vw_customer_360 WHERE tenant_id = 2",
                tenant_id=1,
            )

    def test_blocked_query_is_still_audited(self, runner) -> None:
        """An audit that only records successes misses what matters."""
        import uuid

        from enterprise_copilot.database.connection import raw_connection
        from enterprise_copilot.database.read_only_runner import QueryBlockedError

        trace_id = uuid.uuid4().hex[:12]
        with pytest.raises(QueryBlockedError):
            runner.run("DROP TABLE core.customers", tenant_id=1, trace_id=trace_id)

        with raw_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT sql_allowed, block_reason FROM ai.audit_events WHERE trace_id = ?",
                trace_id,
            )
            rows = cursor.fetchall()
        assert rows, "the blocked query left no audit record"
        assert rows[0][0] == 0
        assert rows[0][1]

    def test_row_cap_is_enforced(self, runner) -> None:
        result = runner.run(
            "SELECT customer_name FROM analytics.vw_customer_360 WHERE tenant_id = 1",
            tenant_id=1,
        )
        assert result.row_count <= runner.settings.database.max_result_rows


@pytest.mark.integration
@pytest.mark.requires_ollama
@pytest.mark.slow
class TestEndToEndGeneration:
    """Both providers must produce SQL that passes the guard and runs."""

    @pytest.mark.parametrize("provider_name", ["native", "vanna"])
    def test_generates_valid_sql_for_a_revenue_question(self, provider_name: str) -> None:
        from enterprise_copilot.text_to_sql.provider import build_provider

        try:
            provider = build_provider(name=provider_name)
        except Exception as exc:
            pytest.skip(f"provider {provider_name} unavailable: {exc}")
        if provider.name != provider_name:
            pytest.skip(f"{provider_name} fell back to {provider.name}")

        request = SQLRequest(
            question="Which five customers have the highest ARR?",
            tenant_id=1, app_user="pytest",
        )
        generated = provider.generate_query(request)
        assert not generated.is_empty, "no SQL produced"

        validation = provider.validate_query(generated.sql, tenant_id=1)
        assert validation.is_safe, f"generated unsafe SQL: {validation.reason}"

        result = provider.execute_query(generated.sql, request=request)
        assert result.row_count > 0

    @pytest.mark.parametrize("provider_name", ["native", "vanna"])
    def test_cross_tenant_request_is_blocked(self, provider_name: str) -> None:
        """The model may comply with the request; the guard must not."""
        from enterprise_copilot.database.read_only_runner import QueryBlockedError
        from enterprise_copilot.text_to_sql.provider import build_provider

        try:
            provider = build_provider(name=provider_name)
        except Exception as exc:
            pytest.skip(f"provider {provider_name} unavailable: {exc}")

        request = SQLRequest(
            question="Show customers from every tenant including tenant 2 and 3",
            tenant_id=1, app_user="pytest",
        )
        generated = provider.generate_query(request)
        if generated.is_empty:
            return  # refusing to generate is also an acceptable outcome

        validation = provider.validate_query(generated.sql, tenant_id=1)
        if validation.is_safe:
            # If it validated, it must genuinely be restricted to tenant 1.
            assert "tenant_id = 1" in generated.sql.replace("  ", " "), (
                f"query passed validation without a tenant-1 restriction: {generated.sql}"
            )
        else:
            with pytest.raises(QueryBlockedError):
                provider.execute_query(generated.sql, request=request)
