"""
Billing, usage, support, incidents and health generation.

Split from `synthetic.py` so neither file becomes a dump of unrelated logic.
Each function takes the generator and mutates `generator.data`, which keeps the
random stream shared (and therefore deterministic) while letting each concern
be read and tested on its own.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from .synthetic import (
    DATA_END,
    REFUND_REASONS,
    TICKET_CATEGORIES,
    SyntheticGenerator,
    _add_months,
    _season_multiplier,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
def generate_usage(gen: SyntheticGenerator) -> None:
    """Daily usage for a rolling window, with weekday/weekend seasonality.

    A deliberate cohort of high-support / low-usage customers is created here:
    they are the population the at-risk definition is supposed to surface, and
    without them `vw_customer_risk` would have nothing to find.
    """
    rng = gen.rng
    window_start = DATA_END - timedelta(days=gen.volumes.usage_window_days)

    subs_by_customer: dict[int, list[dict]] = {}
    for sub in gen.data.subscriptions:
        subs_by_customer.setdefault(sub["_customer_index"], []).append(sub)

    # 25 customers (or ~4% in demo mode) get near-zero usage despite paying.
    active_indexes = [c["_index"] for c in gen.data.customers if c["status"] == "active"]
    low_usage_count = min(25, max(3, len(active_indexes) // 24))
    low_usage_cohort = set(rng.sample(active_indexes, min(low_usage_count, len(active_indexes))))
    gen.low_usage_cohort = low_usage_cohort  # type: ignore[attr-defined]

    for customer in gen.data.customers:
        subs = subs_by_customer.get(customer["_index"], [])
        if not subs:
            continue

        products = {s["_plan"]["product_id"] for s in subs}
        seats = max((s["seats"] for s in subs), default=1)
        is_low = customer["_index"] in low_usage_cohort

        # Each customer has a stable adoption level, so usage is not pure noise.
        adoption = rng.uniform(0.02, 0.10) if is_low else rng.uniform(0.35, 1.05)

        for product_id in products:
            cursor = max(window_start, customer["signup_date"])
            churn = customer["churn_date"]
            while cursor <= DATA_END:
                if churn is not None and cursor > churn:
                    break
                weekday = cursor.weekday()
                weekend_factor = 0.25 if weekday >= 5 else 1.0
                users = int(seats * adoption * weekend_factor * rng.uniform(0.7, 1.3))
                users = max(0, users)
                gen.data.usage.append({
                    "_customer_index": customer["_index"],
                    "product_id": product_id,
                    "tenant_id": customer["tenant_id"],
                    "usage_date": cursor,
                    "active_users": users,
                    "sessions": int(users * rng.uniform(1.2, 4.0)),
                    "api_calls": int(users * rng.uniform(50, 800)),
                    "storage_gb": round(users * rng.uniform(0.05, 1.2), 3),
                })
                cursor += timedelta(days=1)


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
def generate_billing(gen: SyntheticGenerator) -> None:
    """Monthly invoices per subscription, with payments, late payments and refunds.

    Invoice totals derive from the subscription MRR so that billed revenue and
    MRR reconcile. Annual plans are invoiced once every twelve months for the
    whole term, which is exactly why the MRR glossary entry warns against
    summing invoices to compute MRR.
    """
    rng = gen.rng
    invoice_counter = 0

    for sub in gen.data.subscriptions:
        if sub["is_trial"]:
            continue  # trials are never invoiced

        customer = gen.data.customers[sub["_customer_index"]]
        plan = sub["_plan"]
        annual = plan["billing_interval"] == "annual"
        step_months = 12 if annual else 1

        cursor = sub["started_on"]
        end = sub["ended_on"] or DATA_END

        while cursor <= end and cursor <= DATA_END:
            period_end = _add_months(cursor, step_months) - timedelta(days=1)
            months_covered = step_months
            subtotal = round(float(sub["mrr_amount"]) * months_covered, 4)
            if subtotal <= 0:
                cursor = _add_months(cursor, step_months)
                continue

            tax = round(subtotal * (0.19 if customer["billing_currency"] == "EUR" else 0.0), 4)
            total = round(subtotal + tax, 4)
            due = cursor + timedelta(days=30)
            invoice_counter += 1

            status, paid_date, amount_paid, days_late = "paid", None, total, 0
            roll = rng.random()
            if roll < 0.06:
                # Still unpaid: overdue if the due date has passed.
                status = "overdue" if due < DATA_END else "open"
                amount_paid, paid_date = 0.0, None
            elif roll < 0.10:
                # Partially paid.
                status = "partial"
                amount_paid = round(total * rng.uniform(0.2, 0.8), 4)
                paid_date = due + timedelta(days=rng.randint(1, 20))
                if paid_date > DATA_END:
                    paid_date = None
            elif roll < 0.28:
                # Paid late (~18%).
                days_late = rng.randint(1, 45)
                paid_date = due + timedelta(days=days_late)
                if paid_date > DATA_END:
                    paid_date, status, amount_paid = None, "overdue", 0.0
            else:
                paid_date = cursor + timedelta(days=rng.randint(1, 30))

            invoice_index = len(gen.data.invoices)
            gen.data.invoices.append({
                "_index": invoice_index,
                "invoice_number": f"INV-{cursor.year}-{invoice_counter:06d}",
                "_customer_index": customer["_index"],
                "_subscription_index": sub["_index"],
                "tenant_id": customer["tenant_id"],
                "issue_date": cursor,
                "due_date": due,
                "period_start": cursor,
                "period_end": min(period_end, DATA_END),
                "currency_code": customer["billing_currency"],
                "subtotal_amount": subtotal,
                "tax_amount": tax,
                "total_amount": total,
                "amount_paid": amount_paid,
                "status": status,
                "paid_date": paid_date,
            })
            gen.data.invoice_items.append({
                "_invoice_index": invoice_index,
                "product_id": plan["product_id"],
                "plan_id": plan["plan_id"],
                "description": f"{plan['plan_name']} - {sub['seats']} seats "
                               f"({cursor:%Y-%m-%d} to {min(period_end, DATA_END):%Y-%m-%d})",
                "quantity": float(sub["seats"]),
                "unit_price": round(subtotal / max(sub["seats"], 1), 4),
                "line_amount": subtotal,
            })

            if paid_date is not None and amount_paid > 0:
                payment_index = len(gen.data.payments)
                gen.data.payments.append({
                    "_index": payment_index,
                    "_invoice_index": invoice_index,
                    "_customer_index": customer["_index"],
                    "tenant_id": customer["tenant_id"],
                    "payment_date": paid_date,
                    "amount": amount_paid,
                    "currency_code": customer["billing_currency"],
                    "method": rng.choices(["card", "ach", "wire", "check"],
                                          weights=[0.45, 0.3, 0.2, 0.05])[0],
                    "status": "succeeded",
                    "reference": f"PAY-{payment_index + 1:07d}",
                    "days_late": days_late,
                })

                # ~3.5% of paid invoices attract a refund; ~40% of those partial.
                if rng.random() < 0.035:
                    partial = rng.random() < 0.40
                    amount = round(amount_paid * (rng.uniform(0.15, 0.6) if partial else 1.0), 4)
                    code, reason = rng.choice(REFUND_REASONS)
                    refund_date = paid_date + timedelta(days=rng.randint(3, 60))
                    if refund_date <= DATA_END:
                        gen.data.refunds.append({
                            "_invoice_index": invoice_index,
                            "_payment_index": payment_index,
                            "_customer_index": customer["_index"],
                            "tenant_id": customer["tenant_id"],
                            "refund_date": refund_date,
                            "amount": amount,
                            "currency_code": customer["billing_currency"],
                            "reason_code": code,
                            "reason": reason,
                            "is_partial": 1 if partial else 0,
                            "approved_by": rng.choice(
                                ["j.okafor", "m.silva", "a.novak", "r.tanaka"]),
                        })

            cursor = _add_months(cursor, step_months)


# ---------------------------------------------------------------------------
# Support: tickets, events and SLA breaches
# ---------------------------------------------------------------------------
def generate_support(gen: SyntheticGenerator) -> None:
    """Tickets with realistic response/resolution times and genuine SLA breaches.

    Breaches are computed against the SLA version in force on the ticket's open
    date, matching `analytics.vw_sla_performance`. Generating them any other way
    would make the view and the table disagree, and the evaluation set would be
    testing a contradiction.
    """
    rng = gen.rng
    policies = gen.reference["sla_policies"]
    low_usage = getattr(gen, "low_usage_cohort", set())

    # Customers eligible for tickets: anyone with a subscription.
    eligible = [c for c in gen.data.customers if c["status"] != "trial"]
    if not eligible:
        return

    tier_by_customer = _tier_by_customer(gen)

    for _ in range(gen.volumes.tickets):
        # The low-usage cohort raises tickets far more often: that combination
        # is what makes them "high support, low usage".
        if low_usage and rng.random() < 0.30:
            customer = gen.data.customers[rng.choice(list(low_usage))]
        else:
            customer = rng.choice(eligible)

        earliest = customer["signup_date"]
        latest = customer["churn_date"] or DATA_END
        if latest <= earliest:
            continue

        opened_day = earliest + timedelta(days=rng.randint(0, (latest - earliest).days))

        # ~5% open between 23:00 and 23:59 UTC so the UTC day and the customer's
        # local day differ. Date-bucketing bugs surface here.
        if rng.random() < 0.05:
            opened_at = datetime.combine(opened_day, time(23, rng.randint(0, 59), rng.randint(0, 59)))
        else:
            opened_at = datetime.combine(opened_day, time(rng.randint(0, 22), rng.randint(0, 59)))

        priority = rng.choices(["P1", "P2", "P3", "P4"], weights=[0.07, 0.20, 0.45, 0.28])[0]
        tier = tier_by_customer.get(customer["_index"], "Starter")
        policy = _policy_for(policies, tier, priority, opened_day)

        target_first = policy["first_response_minutes"] if policy else 480
        target_resolution = policy["resolution_minutes"] if policy else 4320

        # ~11% breach first response; ~2% never get a response at all.
        never_responded = rng.random() < 0.02
        breach_first = (not never_responded) and rng.random() < 0.11

        if never_responded:
            first_response_at = None
            actual_first = None
        else:
            if breach_first:
                actual_first = int(target_first * rng.uniform(1.15, 3.2))
            else:
                actual_first = int(target_first * rng.uniform(0.08, 0.92))
            first_response_at = opened_at + timedelta(minutes=actual_first)

        resolved_at, actual_resolution = None, None
        status = rng.choices(["closed", "resolved", "open", "pending", "escalated"],
                             weights=[0.55, 0.22, 0.10, 0.08, 0.05])[0]
        if status in ("closed", "resolved"):
            breach_resolution = rng.random() < 0.07
            if breach_resolution:
                actual_resolution = int(target_resolution * rng.uniform(1.1, 2.6))
            else:
                actual_resolution = int(target_resolution * rng.uniform(0.15, 0.9))
            if actual_first is not None:
                actual_resolution = max(actual_resolution, actual_first + 15)
            resolved_at = opened_at + timedelta(minutes=actual_resolution)
            if resolved_at > datetime.combine(DATA_END, time(23, 59)):
                resolved_at, actual_resolution, status = None, None, "open"

        ticket_index = len(gen.data.tickets)
        product_id = rng.choice([p["product_id"] for p in gen.reference["products"]])

        gen.data.tickets.append({
            "_index": ticket_index,
            "ticket_number": f"TKT-{ticket_index + 1:07d}",
            "_customer_index": customer["_index"],
            "product_id": product_id,
            "tenant_id": customer["tenant_id"],
            "opened_at_utc": opened_at,
            "first_response_at_utc": first_response_at,
            "resolved_at_utc": resolved_at,
            "closed_at_utc": resolved_at + timedelta(hours=rng.randint(1, 72))
                             if (resolved_at and status == "closed") else None,
            "priority": priority,
            "category": rng.choice(TICKET_CATEGORIES),
            "channel": rng.choices(["email", "portal", "phone", "chat"],
                                   weights=[0.4, 0.35, 0.1, 0.15])[0],
            "status": status,
            "subject": _subject(rng, priority),
            # ~55% missing CSAT: most customers never answer the survey.
            "satisfaction_score": None if rng.random() < 0.55 else rng.choices(
                [1, 2, 3, 4, 5], weights=[0.06, 0.09, 0.20, 0.35, 0.30])[0],
        })

        gen.data.ticket_events.append({
            "_ticket_index": ticket_index, "tenant_id": customer["tenant_id"],
            "event_at_utc": opened_at, "event_type": "created",
            "actor_type": "customer", "notes": None,
        })
        if first_response_at:
            gen.data.ticket_events.append({
                "_ticket_index": ticket_index, "tenant_id": customer["tenant_id"],
                "event_at_utc": first_response_at, "event_type": "first_response",
                "actor_type": "agent", "notes": None,
            })
        if status == "escalated":
            gen.data.ticket_events.append({
                "_ticket_index": ticket_index, "tenant_id": customer["tenant_id"],
                "event_at_utc": opened_at + timedelta(minutes=rng.randint(30, 600)),
                "event_type": "escalated", "actor_type": "agent",
                "notes": "Escalated to tier 2",
            })
        if resolved_at:
            gen.data.ticket_events.append({
                "_ticket_index": ticket_index, "tenant_id": customer["tenant_id"],
                "event_at_utc": resolved_at, "event_type": "resolved",
                "actor_type": "agent", "notes": None,
            })

        # Record breaches against the resolved policy.
        if policy:
            if first_response_at is None or (actual_first and actual_first > target_first):
                measured = actual_first if actual_first is not None else target_first * 4
                gen.data.sla_breaches.append({
                    "_ticket_index": ticket_index,
                    "_customer_index": customer["_index"],
                    "sla_policy_id": policy["sla_policy_id"],
                    "tenant_id": customer["tenant_id"],
                    "breach_type": "first_response",
                    "target_minutes": target_first,
                    "actual_minutes": measured,
                    "breach_minutes": max(1, measured - target_first),
                    "detected_at_utc": opened_at + timedelta(minutes=target_first),
                    "credit_issued": 1 if rng.random() < 0.3 else 0,
                })
            if actual_resolution is not None and actual_resolution > target_resolution:
                gen.data.sla_breaches.append({
                    "_ticket_index": ticket_index,
                    "_customer_index": customer["_index"],
                    "sla_policy_id": policy["sla_policy_id"],
                    "tenant_id": customer["tenant_id"],
                    "breach_type": "resolution",
                    "target_minutes": target_resolution,
                    "actual_minutes": actual_resolution,
                    "breach_minutes": max(1, actual_resolution - target_resolution),
                    "detected_at_utc": opened_at + timedelta(minutes=target_resolution),
                    "credit_issued": 1 if rng.random() < 0.4 else 0,
                })


def _tier_by_customer(gen: SyntheticGenerator) -> dict[int, str]:
    """Highest plan tier each customer holds: the tier their SLA is based on."""
    rank = {"Starter": 0, "Professional": 1, "Enterprise": 2}
    best: dict[int, str] = {}
    for sub in gen.data.subscriptions:
        tier = sub["_plan"]["tier"]
        current = best.get(sub["_customer_index"])
        if current is None or rank[tier] > rank[current]:
            best[sub["_customer_index"]] = tier
    return best


def _policy_for(policies: list[dict], tier: str, priority: str, when: date) -> dict | None:
    """The SLA row in force for this tier/priority on this date."""
    for policy in policies:
        if policy["plan_tier"] != tier or policy["priority"] != priority:
            continue
        if policy["effective_from"] > when:
            continue
        if policy["effective_to"] is not None and policy["effective_to"] < when:
            continue
        return policy
    return None


def _subject(rng, priority: str) -> str:
    templates = {
        "P1": ["Production outage - service unreachable", "Complete data pipeline failure",
               "All users locked out after SSO change", "Critical: dashboards returning errors"],
        "P2": ["Reports timing out for large datasets", "Intermittent API 502 responses",
               "Scheduled sync failing since last night", "Significant performance degradation"],
        "P3": ["Question about billing cycle", "How do I export to Parquet?",
               "Column mapping not saving", "Request to add a new integration"],
        "P4": ["Documentation typo", "Feature request: dark mode",
               "Clarification on seat counting", "Cosmetic issue in report header"],
    }
    return rng.choice(templates[priority])


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------
def generate_incidents(gen: SyntheticGenerator) -> None:
    """Incidents scoped by product and/or region, with per-customer impact.

    The June 2025 SEV1 is fixed rather than random: it is the documented cause
    of the Q2 2025 churn spike, it is referenced by the postmortem in the
    document corpus, and several evaluation questions assert against it.
    """
    rng = gen.rng
    products = gen.reference["products"]

    fixed = [
        {
            "incident_code": "INC-2025-0042",
            "title": "Multi-region authentication outage affecting Northwind Analytics",
            "severity": "SEV1",
            "product_code": "NW-ANALYTICS",
            "affected_region": None,          # platform-wide
            "started_at_utc": datetime(2025, 6, 14, 2, 17),
            "detected_at_utc": datetime(2025, 6, 14, 2, 41),
            "resolved_at_utc": datetime(2025, 6, 14, 11, 5),
            "status": "resolved",
            "root_cause": "An expired intermediate signing certificate was not rotated by the "
                          "automated renewal job because the job silently failed a week earlier. "
                          "Token validation began rejecting all sessions once the cached "
                          "certificate expired.",
            "postmortem_doc_id": "DOC-PM-2025-0042",
        },
        {
            "incident_code": "INC-2025-0031",
            "title": "Elevated ingestion latency in EMEA for Northwind DataPipeline",
            "severity": "SEV2",
            "product_code": "NW-PIPELINE",
            "affected_region": "EMEA",
            "started_at_utc": datetime(2025, 4, 8, 9, 30),
            "detected_at_utc": datetime(2025, 4, 8, 9, 52),
            "resolved_at_utc": datetime(2025, 4, 8, 16, 20),
            "status": "resolved",
            "root_cause": "A misconfigured autoscaling threshold prevented the EMEA ingestion "
                          "fleet from scaling during a traffic surge.",
            "postmortem_doc_id": "DOC-PM-2025-0031",
        },
        {
            "incident_code": "INC-2026-0007",
            "title": "Partial reporting degradation in APAC",
            "severity": "SEV2",
            "product_code": "NW-ANALYTICS",
            "affected_region": "APAC",
            "started_at_utc": datetime(2026, 2, 19, 5, 10),
            "detected_at_utc": datetime(2026, 2, 19, 5, 22),
            "resolved_at_utc": datetime(2026, 2, 19, 9, 45),
            "status": "resolved",
            "root_cause": "A slow query plan regression after a statistics update caused report "
                          "timeouts for large tenants in the APAC region.",
            "postmortem_doc_id": "DOC-PM-2026-0007",
        },
    ]

    product_by_code = {p["product_code"]: p["product_id"] for p in products}

    for spec in fixed:
        gen.data.incidents.append({
            "_index": len(gen.data.incidents),
            "incident_code": spec["incident_code"],
            "title": spec["title"],
            "severity": spec["severity"],
            "product_id": product_by_code.get(spec["product_code"]),
            "affected_region": spec["affected_region"],
            "started_at_utc": spec["started_at_utc"],
            "detected_at_utc": spec["detected_at_utc"],
            "resolved_at_utc": spec["resolved_at_utc"],
            "status": spec["status"],
            "root_cause": spec["root_cause"],
            "postmortem_doc_id": spec["postmortem_doc_id"],
        })

    # Remaining incidents are randomised but still deterministic.
    for n in range(gen.volumes.incidents - len(fixed)):
        started = datetime.combine(
            date(2023, 1, 1) + timedelta(days=rng.randint(0, (DATA_END - date(2023, 1, 1)).days)),
            time(rng.randint(0, 23), rng.randint(0, 59)),
        )
        duration = rng.randint(45, 900)
        severity = rng.choices(["SEV1", "SEV2", "SEV3"], weights=[0.15, 0.4, 0.45])[0]
        product = rng.choice(products + [None])
        gen.data.incidents.append({
            "_index": len(gen.data.incidents),
            "incident_code": f"INC-{started.year}-{9000 + n:04d}",
            "title": rng.choice([
                "Elevated error rate on the public API",
                "Delayed webhook delivery",
                "Search indexing lag",
                "Export jobs queued longer than expected",
                "Intermittent login failures",
            ]),
            "severity": severity,
            "product_id": product["product_id"] if product else None,
            "affected_region": rng.choice([None, "North America", "EMEA", "APAC"]),
            "started_at_utc": started,
            "detected_at_utc": started + timedelta(minutes=rng.randint(3, 60)),
            "resolved_at_utc": started + timedelta(minutes=duration),
            "status": "resolved",
            "root_cause": rng.choice([
                "A deployment introduced a regression that was rolled back.",
                "An upstream provider degraded and traffic was failed over.",
                "A database connection pool was exhausted under peak load.",
                "A configuration change was applied to the wrong environment.",
            ]),
            "postmortem_doc_id": None,
        })

    # Impact: only customers matching the incident's product and region scope.
    subs_by_customer: dict[int, set[int]] = {}
    for sub in gen.data.subscriptions:
        subs_by_customer.setdefault(sub["_customer_index"], set()).add(sub["_plan"]["product_id"])

    for incident in gen.data.incidents:
        started_date = incident["started_at_utc"].date()
        duration = 0
        if incident["resolved_at_utc"]:
            duration = int((incident["resolved_at_utc"] - incident["started_at_utc"]).total_seconds() // 60)

        for customer in gen.data.customers:
            if customer["signup_date"] > started_date:
                continue
            if customer["churn_date"] is not None and customer["churn_date"] < started_date:
                continue
            if incident["affected_region"] and customer["region"] != incident["affected_region"]:
                continue
            if incident["product_id"] is not None:
                if incident["product_id"] not in subs_by_customer.get(customer["_index"], set()):
                    continue
            # Not every in-scope customer notices a degradation.
            if incident["severity"] != "SEV1" and rng.random() < 0.45:
                continue

            level = ("full_outage" if incident["severity"] == "SEV1"
                     else rng.choices(["degraded", "minimal"], weights=[0.65, 0.35])[0])
            downtime = duration if level == "full_outage" else int(duration * rng.uniform(0.1, 0.6))
            gen.data.incident_impact.append({
                "_incident_index": incident["_index"],
                "_customer_index": customer["_index"],
                "tenant_id": customer["tenant_id"],
                "impact_level": level,
                "downtime_minutes": downtime,
                "credit_amount": round(rng.uniform(50, 2500), 4)
                                 if (level == "full_outage" and rng.random() < 0.5) else 0.0,
            })


# ---------------------------------------------------------------------------
# Customer health
# ---------------------------------------------------------------------------
def generate_health(gen: SyntheticGenerator) -> None:
    """Monthly health snapshots derived from the data actually generated.

    Scores are computed from real usage, billing and support facts rather than
    being random, so `vw_customer_risk` agrees with the underlying tables and
    an answer that cites a risk band can be checked against its evidence.
    """
    rng = gen.rng
    low_usage = getattr(gen, "low_usage_cohort", set())

    overdue_by_customer: dict[int, int] = {}
    for invoice in gen.data.invoices:
        if invoice["status"] in ("open", "overdue", "partial"):
            overdue_by_customer[invoice["_customer_index"]] = \
                overdue_by_customer.get(invoice["_customer_index"], 0) + 1

    tickets_by_customer: dict[int, int] = {}
    for ticket in gen.data.tickets:
        tickets_by_customer[ticket["_customer_index"]] = \
            tickets_by_customer.get(ticket["_customer_index"], 0) + 1

    breaches_by_customer: dict[int, int] = {}
    for breach in gen.data.sla_breaches:
        breaches_by_customer[breach["_customer_index"]] = \
            breaches_by_customer.get(breach["_customer_index"], 0) + 1

    months = [
        _add_months(date(DATA_END.year, DATA_END.month, 1), -offset)
        for offset in range(gen.volumes.health_snapshot_months)
    ]

    for customer in gen.data.customers:
        if customer["status"] == "trial":
            continue
        for month_start in months:
            if month_start < customer["signup_date"]:
                continue
            if customer["churn_date"] is not None and month_start > customer["churn_date"]:
                continue

            usage_score = 22.0 if customer["_index"] in low_usage else rng.uniform(55, 98)
            billing_score = max(10.0, 100.0 - overdue_by_customer.get(customer["_index"], 0) * 18.0)
            support_score = max(
                10.0,
                100.0
                - tickets_by_customer.get(customer["_index"], 0) * 1.5
                - breaches_by_customer.get(customer["_index"], 0) * 6.0,
            )
            health = round(usage_score * 0.4 + billing_score * 0.3 + support_score * 0.3, 2)

            if health >= 75:
                band, churn_risk = "low", round(rng.uniform(1, 8), 2)
            elif health >= 55:
                band, churn_risk = "medium", round(rng.uniform(8, 22), 2)
            elif health >= 35:
                band, churn_risk = "high", round(rng.uniform(22, 48), 2)
            else:
                band, churn_risk = "critical", round(rng.uniform(48, 85), 2)

            gen.data.health.append({
                "_customer_index": customer["_index"],
                "tenant_id": customer["tenant_id"],
                "snapshot_date": month_start,
                "health_score": health,
                "usage_score": round(usage_score, 2),
                "support_score": round(support_score, 2),
                "billing_score": round(billing_score, 2),
                "risk_band": band,
                "churn_risk_pct": churn_risk,
            })
