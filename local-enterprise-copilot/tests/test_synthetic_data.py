"""
Tests for the synthetic data generator.

The headline property is determinism. The evaluation sets assert concrete
answers, so if the generator drifted between runs those assertions would decay
into noise without anyone noticing. `test_generation_is_deterministic` is what
stops that.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from enterprise_copilot.config import Settings
from enterprise_copilot.database.synthetic import (
    DATA_END,
    DATA_START,
    DUPLICATE_NAME_PAIRS,
    EDGE_CASES,
    SyntheticGenerator,
    Volumes,
)
from enterprise_copilot.database.synthetic_operations import (
    generate_billing,
    generate_health,
    generate_incidents,
    generate_support,
    generate_usage,
)

pytestmark = pytest.mark.integration  # needs reference data from SQL Server


def build(settings: Settings, reference: dict) -> SyntheticGenerator:
    """Run the full pipeline at demo volume."""
    generator = SyntheticGenerator(settings, Volumes.demo(), reference)
    generator.generate_customers()
    generator.generate_subscriptions()
    generate_usage(generator)
    generate_billing(generator)
    generate_incidents(generator)
    generate_support(generator)
    generate_health(generator)
    return generator


def fingerprint(generator: SyntheticGenerator) -> str:
    """A stable hash over the generated rows, ignoring in-memory-only keys."""
    payload = []
    for name in ("customers", "subscriptions", "invoices", "tickets", "sla_breaches"):
        for row in getattr(generator.data, name):
            payload.append(
                {
                    k: (v.isoformat() if hasattr(v, "isoformat") else v)
                    for k, v in sorted(row.items())
                    if not k.startswith("_")
                }
            )
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TestDeterminism:
    def test_generation_is_deterministic(self, settings: Settings, reference_data: dict) -> None:
        """Two runs with the same seed must produce identical data."""
        first = fingerprint(build(settings, reference_data))
        second = fingerprint(build(settings, reference_data))
        assert first == second, (
            "Generator is not deterministic. The evaluation sets assert specific "
            "answers and rely on this property."
        )

    def test_different_seed_produces_different_data(
        self, settings: Settings, reference_data: dict
    ) -> None:
        baseline = fingerprint(build(settings, reference_data))
        altered = settings.model_copy(update={"random_seed": settings.random_seed + 1})
        assert fingerprint(build(altered, reference_data)) != baseline


class TestReferentialIntegrity:
    def test_every_subscription_points_at_a_real_customer(
        self, settings: Settings, reference_data: dict
    ) -> None:
        generator = build(settings, reference_data)
        valid = {c["_index"] for c in generator.data.customers}
        assert all(s["_customer_index"] in valid for s in generator.data.subscriptions)

    def test_tenant_is_consistent_between_customer_and_subscription(
        self, settings: Settings, reference_data: dict
    ) -> None:
        generator = build(settings, reference_data)
        by_index = {c["_index"]: c for c in generator.data.customers}
        mismatches = [
            s
            for s in generator.data.subscriptions
            if s["tenant_id"] != by_index[s["_customer_index"]]["tenant_id"]
        ]
        assert not mismatches, f"{len(mismatches)} subscriptions leaked across tenants"

    def test_invoices_never_reference_a_trial_subscription(
        self, settings: Settings, reference_data: dict
    ) -> None:
        """Trials are excluded from revenue; billing one would corrupt MRR."""
        generator = build(settings, reference_data)
        trials = {s["_index"] for s in generator.data.subscriptions if s["is_trial"]}
        assert not [i for i in generator.data.invoices if i["_subscription_index"] in trials]


class TestBusinessRules:
    def test_churned_customers_have_a_churn_date(
        self, settings: Settings, reference_data: dict
    ) -> None:
        generator = build(settings, reference_data)
        for customer in generator.data.customers:
            if customer["status"] == "churned":
                assert customer["churn_date"] is not None
            else:
                assert customer["churn_date"] is None

    def test_no_negative_mrr(self, settings: Settings, reference_data: dict) -> None:
        generator = build(settings, reference_data)
        assert all(s["mrr_amount"] >= 0 for s in generator.data.subscriptions)

    def test_breach_minutes_are_positive(self, settings: Settings, reference_data: dict) -> None:
        generator = build(settings, reference_data)
        assert all(b["breach_minutes"] > 0 for b in generator.data.sla_breaches)

    def test_dates_stay_inside_the_data_window(
        self, settings: Settings, reference_data: dict
    ) -> None:
        generator = build(settings, reference_data)
        for customer in generator.data.customers:
            assert DATA_START <= customer["signup_date"] <= DATA_END
        for sub in generator.data.subscriptions:
            if sub["ended_on"]:
                assert sub["ended_on"] >= sub["started_on"]
                assert sub["ended_on"] <= DATA_END


class TestEdgeCases:
    """Every documented edge case must actually be present in the data."""

    def test_duplicate_looking_names_present(
        self, settings: Settings, reference_data: dict
    ) -> None:
        generator = build(settings, reference_data)
        names = {c["display_name"] for c in generator.data.customers}
        found = [pair for pair in DUPLICATE_NAME_PAIRS if pair[0] in names and pair[1] in names]
        assert len(found) >= 4, f"only {len(found)} near-duplicate pairs generated"

    def test_missing_optional_values_present(
        self, settings: Settings, reference_data: dict
    ) -> None:
        generator = build(settings, reference_data)
        assert any(c["industry"] is None for c in generator.data.customers)
        assert any(c["employee_count"] is None for c in generator.data.customers)
        assert any(t["satisfaction_score"] is None for t in generator.data.tickets)

    def test_partial_refunds_present(self, settings: Settings, reference_data: dict) -> None:
        generator = build(settings, reference_data)
        if generator.data.refunds:
            assert any(r["is_partial"] == 1 for r in generator.data.refunds)

    def test_unresponded_tickets_count_as_breaches(
        self, settings: Settings, reference_data: dict
    ) -> None:
        """A ticket that never got a reply is a first-response breach, not a zero."""
        generator = build(settings, reference_data)
        never = [t for t in generator.data.tickets if t["first_response_at_utc"] is None]
        if never:
            breached = {
                b["_ticket_index"]
                for b in generator.data.sla_breaches
                if b["breach_type"] == "first_response"
            }
            assert any(t["_index"] in breached for t in never)

    def test_multi_currency_present(self, settings: Settings, reference_data: dict) -> None:
        generator = build(settings, reference_data)
        assert len({c["billing_currency"] for c in generator.data.customers}) >= 2

    def test_timezone_boundary_tickets_present(
        self, settings: Settings, reference_data: dict
    ) -> None:
        generator = build(settings, reference_data)
        late = [t for t in generator.data.tickets if t["opened_at_utc"].hour == 23]
        assert late, "no tickets opened in the 23:00 UTC hour; time-zone edge case missing"

    def test_every_documented_edge_case_has_a_description(self) -> None:
        assert len(EDGE_CASES) >= 15
        assert all(isinstance(v, str) and v for v in EDGE_CASES.values())


class TestIncidents:
    def test_june_2025_sev1_is_fixed_not_random(
        self, settings: Settings, reference_data: dict
    ) -> None:
        """The evaluation set and the postmortem document both reference this."""
        generator = build(settings, reference_data)
        codes = {i["incident_code"] for i in generator.data.incidents}
        assert "INC-2025-0042" in codes

        incident = next(
            i for i in generator.data.incidents if i["incident_code"] == "INC-2025-0042"
        )
        assert incident["severity"] == "SEV1"
        assert incident["started_at_utc"].date() == date(2025, 6, 14)
        assert incident["postmortem_doc_id"] == "DOC-PM-2025-0042"

    def test_regional_incident_only_affects_that_region(
        self, settings: Settings, reference_data: dict
    ) -> None:
        generator = build(settings, reference_data)
        by_index = {c["_index"]: c for c in generator.data.customers}
        incidents = {i["_index"]: i for i in generator.data.incidents}

        for impact in generator.data.incident_impact:
            incident = incidents[impact["_incident_index"]]
            if incident["affected_region"]:
                customer = by_index[impact["_customer_index"]]
                assert customer["region"] == incident["affected_region"]
