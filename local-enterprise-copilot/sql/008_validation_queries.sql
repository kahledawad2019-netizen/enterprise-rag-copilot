/* ===========================================================================
   008_validation_queries.sql
   Schema version: 1.0.0

   Proves the generated dataset is internally consistent and contains the
   patterns the evaluation set depends on.

   Every query returns one row shaped as:
       check_name | expected | actual | status ('PASS' | 'FAIL')

   `scripts/validate_data.py` runs this file and fails the build on any FAIL,
   so these are executable assertions rather than documentation.

   The point is not that the numbers are pretty. It is that a question like
   "which customers had more than three SLA breaches" has a stable, verifiable
   answer, which is what makes the evaluation set meaningful.
   =========================================================================== */

USE [EnterpriseCopilot];
GO

SET NOCOUNT ON;
GO

/* ---------------------------------------------------------------------------
   1. Row counts - is there enough data to be interesting?
   --------------------------------------------------------------------------- */
SELECT 'row_count.customers' AS check_name, '>= 500' AS expected,
       CAST(COUNT(*) AS varchar(20)) AS actual,
       CASE WHEN COUNT(*) >= 500 THEN 'PASS' ELSE 'FAIL' END AS status
FROM core.customers
UNION ALL
SELECT 'row_count.subscriptions', '>= 700', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 700 THEN 'PASS' ELSE 'FAIL' END
FROM core.subscriptions
UNION ALL
SELECT 'row_count.invoices', '>= 5000', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 5000 THEN 'PASS' ELSE 'FAIL' END
FROM billing.invoices
UNION ALL
SELECT 'row_count.tickets', '>= 3000', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 3000 THEN 'PASS' ELSE 'FAIL' END
FROM support.tickets
UNION ALL
SELECT 'row_count.usage_daily', '>= 100000', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 100000 THEN 'PASS' ELSE 'FAIL' END
FROM core.usage_daily
UNION ALL
SELECT 'row_count.glossary_current', '= 19', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 19 THEN 'PASS' ELSE 'FAIL' END
FROM ai.business_glossary WHERE is_current = 1

/* ---------------------------------------------------------------------------
   2. Referential integrity - orphans must be impossible.
   The foreign keys enforce this, so a non-zero result means something has
   bypassed them and the dataset cannot be trusted.
   --------------------------------------------------------------------------- */
UNION ALL
SELECT 'integrity.subscriptions_orphan_customer', '0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM core.subscriptions s
LEFT JOIN core.customers c ON c.customer_id = s.customer_id
WHERE c.customer_id IS NULL
UNION ALL
SELECT 'integrity.invoices_orphan_customer', '0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM billing.invoices i
LEFT JOIN core.customers c ON c.customer_id = i.customer_id
WHERE c.customer_id IS NULL
UNION ALL
SELECT 'integrity.breaches_orphan_ticket', '0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM support.sla_breaches b
LEFT JOIN support.tickets t ON t.ticket_id = b.ticket_id
WHERE t.ticket_id IS NULL
UNION ALL
/* Tenant consistency: a subscription must belong to its customer's tenant.
   Nothing in the schema enforces this, so it is checked explicitly. A leak
   here would silently break tenant isolation. */
SELECT 'integrity.subscription_tenant_matches_customer', '0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM core.subscriptions s
JOIN core.customers c ON c.customer_id = s.customer_id
WHERE s.tenant_id <> c.tenant_id
UNION ALL
SELECT 'integrity.ticket_tenant_matches_customer', '0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM support.tickets t
JOIN core.customers c ON c.customer_id = t.customer_id
WHERE t.tenant_id <> c.tenant_id

/* ---------------------------------------------------------------------------
   3. Business-rule invariants
   --------------------------------------------------------------------------- */
UNION ALL
/* A churned customer has a churn date; an unchurned one does not. Enforced by
   CK_customers_churn, verified here so a constraint drop is caught. */
SELECT 'rule.churned_customers_have_churn_date', '0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM core.customers
WHERE (status = 'churned' AND churn_date IS NULL)
   OR (status <> 'churned' AND churn_date IS NOT NULL)
UNION ALL
SELECT 'rule.no_negative_mrr', '0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM core.subscriptions WHERE mrr_amount < 0
UNION ALL
SELECT 'rule.invoice_paid_not_exceeding_total', '0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM billing.invoices WHERE amount_paid > total_amount + 0.01
UNION ALL
SELECT 'rule.breach_minutes_positive', '0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM support.sla_breaches WHERE breach_minutes <= 0
UNION ALL
/* Trials must never carry revenue: the MRR definition depends on it. */
SELECT 'rule.trials_excluded_from_mrr_view', '0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM core.subscriptions s
WHERE s.is_trial = 1
  AND EXISTS (SELECT 1 FROM billing.invoices i WHERE i.subscription_id = s.subscription_id)

/* ---------------------------------------------------------------------------
   4. Expected ranges - the data must look like a real SaaS business.
   --------------------------------------------------------------------------- */
UNION ALL
SELECT 'range.total_mrr_plausible', 'between 100k and 5m',
       CAST(CAST(SUM(mrr_amount) AS decimal(19,2)) AS varchar(30)),
       CASE WHEN SUM(mrr_amount) BETWEEN 100000 AND 5000000 THEN 'PASS' ELSE 'FAIL' END
FROM core.subscriptions WHERE status = 'active' AND is_trial = 0
UNION ALL
SELECT 'range.active_customer_share', 'between 40 and 95 pct',
       CAST(CAST(100.0 * SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) / COUNT(*)
            AS decimal(5,1)) AS varchar(10)),
       CASE WHEN 100.0 * SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) / COUNT(*)
                 BETWEEN 40 AND 95 THEN 'PASS' ELSE 'FAIL' END
FROM core.customers
UNION ALL
SELECT 'range.sla_breach_rate', 'between 3 and 30 pct',
       CAST(CAST(100.0 * COUNT(DISTINCT b.ticket_id) / NULLIF((SELECT COUNT(*) FROM support.tickets), 0)
            AS decimal(5,1)) AS varchar(10)),
       CASE WHEN 100.0 * COUNT(DISTINCT b.ticket_id)
                 / NULLIF((SELECT COUNT(*) FROM support.tickets), 0)
                 BETWEEN 3 AND 30 THEN 'PASS' ELSE 'FAIL' END
FROM support.sla_breaches b
UNION ALL
SELECT 'range.late_payment_rate', 'between 5 and 40 pct',
       CAST(CAST(100.0 * SUM(CASE WHEN days_late > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)
            AS decimal(5,1)) AS varchar(10)),
       CASE WHEN 100.0 * SUM(CASE WHEN days_late > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)
                 BETWEEN 5 AND 40 THEN 'PASS' ELSE 'FAIL' END
FROM billing.payments

/* ---------------------------------------------------------------------------
   5. Deliberate edge cases - each must actually be present.
   --------------------------------------------------------------------------- */
UNION ALL
SELECT 'edge.duplicate_looking_names', '>= 6', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 6 THEN 'PASS' ELSE 'FAIL' END
FROM (
    SELECT LEFT(display_name, 5) AS prefix
    FROM core.customers
    GROUP BY LEFT(display_name, 5)
    HAVING COUNT(*) > 1
) AS dupes
UNION ALL
SELECT 'edge.missing_industry_values', '>= 20', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 20 THEN 'PASS' ELSE 'FAIL' END
FROM core.customers WHERE industry IS NULL
UNION ALL
SELECT 'edge.partial_refunds_present', '>= 10', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 10 THEN 'PASS' ELSE 'FAIL' END
FROM billing.refunds WHERE is_partial = 1
UNION ALL
SELECT 'edge.reactivated_customers_present', '>= 1', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 1 THEN 'PASS' ELSE 'FAIL' END
FROM core.customers WHERE is_reactivated = 1
UNION ALL
SELECT 'edge.plan_migrations_present', '>= 50', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 50 THEN 'PASS' ELSE 'FAIL' END
FROM core.subscription_changes WHERE change_type IN ('upgrade', 'downgrade')
UNION ALL
SELECT 'edge.timezone_boundary_tickets', '>= 50', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 50 THEN 'PASS' ELSE 'FAIL' END
FROM support.tickets WHERE DATEPART(HOUR, opened_at_utc) = 23
UNION ALL
SELECT 'edge.multi_currency_present', '>= 2', CAST(COUNT(DISTINCT billing_currency) AS varchar(20)),
       CASE WHEN COUNT(DISTINCT billing_currency) >= 2 THEN 'PASS' ELSE 'FAIL' END
FROM core.customers
UNION ALL
SELECT 'edge.all_tenants_populated', '= 3', CAST(COUNT(DISTINCT tenant_id) AS varchar(20)),
       CASE WHEN COUNT(DISTINCT tenant_id) = 3 THEN 'PASS' ELSE 'FAIL' END
FROM core.customers
UNION ALL
SELECT 'edge.trial_only_customers_present', '>= 10', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 10 THEN 'PASS' ELSE 'FAIL' END
FROM core.customers WHERE status = 'trial'
UNION ALL
SELECT 'edge.unpaid_invoices_present', '>= 100', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 100 THEN 'PASS' ELSE 'FAIL' END
FROM billing.invoices WHERE status IN ('open', 'overdue', 'partial')

/* ---------------------------------------------------------------------------
   6. Known facts the evaluation set asserts against.
   If any of these change, evals/text_to_sql.jsonl must be regenerated.
   --------------------------------------------------------------------------- */
UNION ALL
SELECT 'known.june_2025_sev1_exists', '= 1', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 1 THEN 'PASS' ELSE 'FAIL' END
FROM support.incidents
WHERE incident_code = 'INC-2025-0042' AND severity = 'SEV1'
UNION ALL
SELECT 'known.june_2025_sev1_has_affected_customers', '>= 20', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 20 THEN 'PASS' ELSE 'FAIL' END
FROM support.incident_impact im
JOIN support.incidents i ON i.incident_id = im.incident_id
WHERE i.incident_code = 'INC-2025-0042'
UNION ALL
SELECT 'known.sla_has_two_versions', '= 2', CAST(COUNT(DISTINCT version) AS varchar(20)),
       CASE WHEN COUNT(DISTINCT version) = 2 THEN 'PASS' ELSE 'FAIL' END
FROM support.sla_policies
UNION ALL
/* The Enterprise P1 first-response target tightened from 30 to 15 minutes on
   2025-01-01. Version-sensitive evaluation questions depend on both existing. */
SELECT 'known.enterprise_p1_target_tightened', '30 -> 15',
       CONCAT(MAX(CASE WHEN version = '1.0' THEN first_response_minutes END), ' -> ',
              MAX(CASE WHEN version = '2.0' THEN first_response_minutes END)),
       CASE WHEN MAX(CASE WHEN version = '1.0' THEN first_response_minutes END) = 30
             AND MAX(CASE WHEN version = '2.0' THEN first_response_minutes END) = 15
            THEN 'PASS' ELSE 'FAIL' END
FROM support.sla_policies WHERE policy_code = 'SLA-ENT-P1'
UNION ALL
SELECT 'known.superseded_mrr_definition_exists', '= 1', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) = 1 THEN 'PASS' ELSE 'FAIL' END
FROM ai.business_glossary WHERE term = 'MRR' AND is_current = 0
UNION ALL
SELECT 'known.customers_with_more_than_3_breaches', '>= 5', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 5 THEN 'PASS' ELSE 'FAIL' END
FROM (
    SELECT customer_id FROM support.sla_breaches
    GROUP BY customer_id HAVING COUNT(*) > 3
) AS heavy
UNION ALL
SELECT 'known.at_risk_customers_present', '>= 10', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) >= 10 THEN 'PASS' ELSE 'FAIL' END
FROM analytics.vw_customer_risk WHERE risk_signal_count >= 2

/* ---------------------------------------------------------------------------
   7. Analytics views must all be queryable and non-empty.
   A view that compiles but returns nothing is a silent failure.
   --------------------------------------------------------------------------- */
UNION ALL
SELECT 'view.vw_customer_360', '> 0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) > 0 THEN 'PASS' ELSE 'FAIL' END FROM analytics.vw_customer_360
UNION ALL
SELECT 'view.vw_monthly_recurring_revenue', '> 0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) > 0 THEN 'PASS' ELSE 'FAIL' END FROM analytics.vw_monthly_recurring_revenue
UNION ALL
SELECT 'view.vw_churn_metrics', '> 0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) > 0 THEN 'PASS' ELSE 'FAIL' END FROM analytics.vw_churn_metrics
UNION ALL
SELECT 'view.vw_sla_performance', '> 0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) > 0 THEN 'PASS' ELSE 'FAIL' END FROM analytics.vw_sla_performance
UNION ALL
SELECT 'view.vw_customer_risk', '> 0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) > 0 THEN 'PASS' ELSE 'FAIL' END FROM analytics.vw_customer_risk
UNION ALL
SELECT 'view.vw_incident_impact', '> 0', CAST(COUNT(*) AS varchar(20)),
       CASE WHEN COUNT(*) > 0 THEN 'PASS' ELSE 'FAIL' END FROM analytics.vw_incident_impact
ORDER BY check_name;
GO
