"""
Bulk-load generated data into SQL Server.

The generator works with list indexes (`_customer_index`) rather than database
keys, because the keys do not exist until the rows are inserted. This module
inserts in dependency order and builds index -> identity maps as it goes.

`fast_executemany` turns tens of thousands of single INSERTs into a handful of
batched round trips; without it, loading takes minutes instead of seconds.
"""

from __future__ import annotations

import logging
from typing import Any

from .synthetic import GeneratedData

log = logging.getLogger(__name__)

# Child-to-parent order, so truncation never violates a foreign key.
DELETE_ORDER = [
    "analytics.customer_health",
    "support.incident_impact",
    "support.sla_breaches",
    "support.ticket_events",
    "support.tickets",
    "support.incidents",
    "billing.refunds",
    "billing.payments",
    "billing.invoice_items",
    "billing.invoices",
    "core.usage_daily",
    "core.subscription_changes",
    "core.subscriptions",
    "core.customer_contacts",
    "core.customers",
]


class SyntheticLoader:
    def __init__(self, connection: Any) -> None:
        self.conn = connection
        self.cursor = connection.cursor()
        self.cursor.fast_executemany = True
        self.customer_ids: dict[int, int] = {}
        self.subscription_ids: dict[int, int] = {}
        self.invoice_ids: dict[int, int] = {}
        self.payment_ids: dict[int, int] = {}
        self.ticket_ids: dict[int, int] = {}
        self.incident_ids: dict[int, int] = {}

    # -- utilities ---------------------------------------------------------
    def clear_transactional_data(self) -> None:
        """Remove generated rows, leaving reference data from 007 intact."""
        for table in DELETE_ORDER:
            self.cursor.execute(f"DELETE FROM {table};")
        self.conn.commit()
        log.info("Cleared %d transactional tables", len(DELETE_ORDER))

    def _insert_many(self, table: str, columns: list[str], rows: list[tuple]) -> None:
        if not rows:
            return
        placeholders = ", ".join("?" for _ in columns)
        column_list = ", ".join(f"[{c}]" for c in columns)
        sql = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"
        self.cursor.executemany(sql, rows)

    def _fetch_identity_map(self, table: str, key_column: str, id_column: str) -> dict[str, int]:
        self.cursor.execute(f"SELECT {key_column}, {id_column} FROM {table}")
        return {row[0]: row[1] for row in self.cursor.fetchall()}

    # -- load steps --------------------------------------------------------
    def load(self, data: GeneratedData) -> dict[str, int]:
        self._load_customers(data)
        self._load_contacts(data)
        self._load_subscriptions(data)
        self._load_subscription_changes(data)
        self._load_usage(data)
        self._load_invoices(data)
        self._load_invoice_items(data)
        self._load_payments(data)
        self._load_refunds(data)
        self._load_incidents(data)
        self._load_tickets(data)
        self._load_ticket_events(data)
        self._load_sla_breaches(data)
        self._load_incident_impact(data)
        self._load_health(data)
        self.conn.commit()
        return data.counts()

    def _load_customers(self, data: GeneratedData) -> None:
        cols = ["tenant_id", "customer_code", "legal_name", "display_name", "industry",
                "segment", "country_code", "region", "employee_count", "billing_currency",
                "time_zone", "signup_date", "churn_date", "status", "is_reactivated"]
        rows = [tuple(c[col] for col in cols) for c in data.customers]
        self._insert_many("core.customers", cols, rows)
        self.conn.commit()

        code_map = self._fetch_identity_map("core.customers", "customer_code", "customer_id")
        for customer in data.customers:
            self.customer_ids[customer["_index"]] = code_map[customer["customer_code"]]
        log.info("Loaded %d customers", len(rows))

    def _load_contacts(self, data: GeneratedData) -> None:
        cols = ["customer_id", "tenant_id", "full_name", "email", "phone",
                "role_title", "is_primary", "is_synthetic"]
        rows = [
            (self.customer_ids[c["_customer_index"]], c["tenant_id"], c["full_name"],
             c["email"], c["phone"], c["role_title"], c["is_primary"], c["is_synthetic"])
            for c in data.contacts
        ]
        self._insert_many("core.customer_contacts", cols, rows)
        log.info("Loaded %d contacts", len(rows))

    def _load_subscriptions(self, data: GeneratedData) -> None:
        cols = ["customer_id", "plan_id", "tenant_id", "started_on", "ended_on", "status",
                "seats", "mrr_amount", "discount_pct", "currency_code", "is_trial",
                "auto_renew", "cancellation_reason"]
        rows = [
            (self.customer_ids[s["_customer_index"]], s["plan_id"], s["tenant_id"],
             s["started_on"], s["ended_on"], s["status"], s["seats"], s["mrr_amount"],
             s["discount_pct"], s["currency_code"], s["is_trial"], s["auto_renew"],
             s["cancellation_reason"])
            for s in data.subscriptions
        ]
        self._insert_many("core.subscriptions", cols, rows)
        self.conn.commit()

        # Subscriptions have no natural key, so map by insertion order, which
        # IDENTITY preserves for a single ordered executemany batch.
        self.cursor.execute("SELECT subscription_id FROM core.subscriptions ORDER BY subscription_id")
        ids = [row[0] for row in self.cursor.fetchall()]
        for subscription, identity in zip(data.subscriptions, ids, strict=True):
            self.subscription_ids[subscription["_index"]] = identity
        log.info("Loaded %d subscriptions", len(rows))

    def _load_subscription_changes(self, data: GeneratedData) -> None:
        cols = ["subscription_id", "customer_id", "tenant_id", "change_type", "changed_on",
                "from_plan_id", "to_plan_id", "from_seats", "to_seats", "from_mrr",
                "to_mrr", "mrr_delta", "reason"]
        rows = [
            (self.subscription_ids[c["_subscription_index"]],
             self.customer_ids[c["_customer_index"]], c["tenant_id"], c["change_type"],
             c["changed_on"], c["from_plan_id"], c["to_plan_id"], c["from_seats"],
             c["to_seats"], c["from_mrr"], c["to_mrr"], c["mrr_delta"], c["reason"])
            for c in data.subscription_changes
        ]
        self._insert_many("core.subscription_changes", cols, rows)
        log.info("Loaded %d subscription changes", len(rows))

    def _load_usage(self, data: GeneratedData) -> None:
        cols = ["customer_id", "product_id", "tenant_id", "usage_date",
                "active_users", "sessions", "api_calls", "storage_gb"]
        rows = [
            (self.customer_ids[u["_customer_index"]], u["product_id"], u["tenant_id"],
             u["usage_date"], u["active_users"], u["sessions"], u["api_calls"], u["storage_gb"])
            for u in data.usage
        ]
        # Chunked: a single 300k-row executemany can exhaust the driver's buffer.
        for start in range(0, len(rows), 20000):
            self._insert_many("core.usage_daily", cols, rows[start:start + 20000])
            self.conn.commit()
        log.info("Loaded %d usage rows", len(rows))

    def _load_invoices(self, data: GeneratedData) -> None:
        cols = ["invoice_number", "customer_id", "subscription_id", "tenant_id",
                "issue_date", "due_date", "period_start", "period_end", "currency_code",
                "subtotal_amount", "tax_amount", "total_amount", "amount_paid",
                "status", "paid_date"]
        rows = [
            (i["invoice_number"], self.customer_ids[i["_customer_index"]],
             self.subscription_ids[i["_subscription_index"]], i["tenant_id"],
             i["issue_date"], i["due_date"], i["period_start"], i["period_end"],
             i["currency_code"], i["subtotal_amount"], i["tax_amount"], i["total_amount"],
             i["amount_paid"], i["status"], i["paid_date"])
            for i in data.invoices
        ]
        for start in range(0, len(rows), 20000):
            self._insert_many("billing.invoices", cols, rows[start:start + 20000])
        self.conn.commit()

        number_map = self._fetch_identity_map("billing.invoices", "invoice_number", "invoice_id")
        for invoice in data.invoices:
            self.invoice_ids[invoice["_index"]] = number_map[invoice["invoice_number"]]
        log.info("Loaded %d invoices", len(rows))

    def _load_invoice_items(self, data: GeneratedData) -> None:
        cols = ["invoice_id", "product_id", "plan_id", "description",
                "quantity", "unit_price", "line_amount"]
        rows = [
            (self.invoice_ids[it["_invoice_index"]], it["product_id"], it["plan_id"],
             it["description"], it["quantity"], it["unit_price"], it["line_amount"])
            for it in data.invoice_items
        ]
        for start in range(0, len(rows), 20000):
            self._insert_many("billing.invoice_items", cols, rows[start:start + 20000])
        self.conn.commit()
        log.info("Loaded %d invoice items", len(rows))

    def _load_payments(self, data: GeneratedData) -> None:
        cols = ["invoice_id", "customer_id", "tenant_id", "payment_date", "amount",
                "currency_code", "method", "status", "reference", "days_late"]
        rows = [
            (self.invoice_ids[p["_invoice_index"]], self.customer_ids[p["_customer_index"]],
             p["tenant_id"], p["payment_date"], p["amount"], p["currency_code"],
             p["method"], p["status"], p["reference"], p["days_late"])
            for p in data.payments
        ]
        for start in range(0, len(rows), 20000):
            self._insert_many("billing.payments", cols, rows[start:start + 20000])
        self.conn.commit()

        ref_map = self._fetch_identity_map("billing.payments", "reference", "payment_id")
        for payment in data.payments:
            self.payment_ids[payment["_index"]] = ref_map[payment["reference"]]
        log.info("Loaded %d payments", len(rows))

    def _load_refunds(self, data: GeneratedData) -> None:
        cols = ["invoice_id", "payment_id", "customer_id", "tenant_id", "refund_date",
                "amount", "currency_code", "reason_code", "reason", "is_partial", "approved_by"]
        rows = [
            (self.invoice_ids[r["_invoice_index"]], self.payment_ids.get(r["_payment_index"]),
             self.customer_ids[r["_customer_index"]], r["tenant_id"], r["refund_date"],
             r["amount"], r["currency_code"], r["reason_code"], r["reason"],
             r["is_partial"], r["approved_by"])
            for r in data.refunds
        ]
        self._insert_many("billing.refunds", cols, rows)
        log.info("Loaded %d refunds", len(rows))

    def _load_incidents(self, data: GeneratedData) -> None:
        cols = ["incident_code", "title", "severity", "product_id", "affected_region",
                "started_at_utc", "detected_at_utc", "resolved_at_utc", "status",
                "root_cause", "postmortem_doc_id"]
        rows = [tuple(i[c] for c in cols) for i in data.incidents]
        self._insert_many("support.incidents", cols, rows)
        self.conn.commit()

        code_map = self._fetch_identity_map("support.incidents", "incident_code", "incident_id")
        for incident in data.incidents:
            self.incident_ids[incident["_index"]] = code_map[incident["incident_code"]]
        log.info("Loaded %d incidents", len(rows))

    def _load_tickets(self, data: GeneratedData) -> None:
        cols = ["ticket_number", "customer_id", "product_id", "tenant_id", "opened_at_utc",
                "first_response_at_utc", "resolved_at_utc", "closed_at_utc", "priority",
                "category", "channel", "status", "subject", "satisfaction_score"]
        rows = [
            (t["ticket_number"], self.customer_ids[t["_customer_index"]], t["product_id"],
             t["tenant_id"], t["opened_at_utc"], t["first_response_at_utc"],
             t["resolved_at_utc"], t["closed_at_utc"], t["priority"], t["category"],
             t["channel"], t["status"], t["subject"], t["satisfaction_score"])
            for t in data.tickets
        ]
        for start in range(0, len(rows), 20000):
            self._insert_many("support.tickets", cols, rows[start:start + 20000])
        self.conn.commit()

        number_map = self._fetch_identity_map("support.tickets", "ticket_number", "ticket_id")
        for ticket in data.tickets:
            self.ticket_ids[ticket["_index"]] = number_map[ticket["ticket_number"]]
        log.info("Loaded %d tickets", len(rows))

    def _load_ticket_events(self, data: GeneratedData) -> None:
        cols = ["ticket_id", "tenant_id", "event_at_utc", "event_type", "actor_type", "notes"]
        rows = [
            (self.ticket_ids[e["_ticket_index"]], e["tenant_id"], e["event_at_utc"],
             e["event_type"], e["actor_type"], e["notes"])
            for e in data.ticket_events
        ]
        for start in range(0, len(rows), 20000):
            self._insert_many("support.ticket_events", cols, rows[start:start + 20000])
        self.conn.commit()
        log.info("Loaded %d ticket events", len(rows))

    def _load_sla_breaches(self, data: GeneratedData) -> None:
        cols = ["ticket_id", "customer_id", "sla_policy_id", "tenant_id", "breach_type",
                "target_minutes", "actual_minutes", "breach_minutes", "detected_at_utc",
                "credit_issued"]
        rows = [
            (self.ticket_ids[b["_ticket_index"]], self.customer_ids[b["_customer_index"]],
             b["sla_policy_id"], b["tenant_id"], b["breach_type"], b["target_minutes"],
             b["actual_minutes"], b["breach_minutes"], b["detected_at_utc"], b["credit_issued"])
            for b in data.sla_breaches
        ]
        self._insert_many("support.sla_breaches", cols, rows)
        log.info("Loaded %d SLA breaches", len(rows))

    def _load_incident_impact(self, data: GeneratedData) -> None:
        cols = ["incident_id", "customer_id", "tenant_id", "impact_level",
                "downtime_minutes", "credit_amount"]
        rows = [
            (self.incident_ids[i["_incident_index"]], self.customer_ids[i["_customer_index"]],
             i["tenant_id"], i["impact_level"], i["downtime_minutes"], i["credit_amount"])
            for i in data.incident_impact
        ]
        for start in range(0, len(rows), 20000):
            self._insert_many("support.incident_impact", cols, rows[start:start + 20000])
        self.conn.commit()
        log.info("Loaded %d incident impacts", len(rows))

    def _load_health(self, data: GeneratedData) -> None:
        cols = ["customer_id", "tenant_id", "snapshot_date", "health_score", "usage_score",
                "support_score", "billing_score", "risk_band", "churn_risk_pct"]
        rows = [
            (self.customer_ids[h["_customer_index"]], h["tenant_id"], h["snapshot_date"],
             h["health_score"], h["usage_score"], h["support_score"], h["billing_score"],
             h["risk_band"], h["churn_risk_pct"])
            for h in data.health
        ]
        for start in range(0, len(rows), 20000):
            self._insert_many("analytics.customer_health", cols, rows[start:start + 20000])
        self.conn.commit()
        log.info("Loaded %d health snapshots", len(rows))
