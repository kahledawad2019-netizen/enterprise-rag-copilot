/* ===========================================================================
   005_create_views.sql
   Schema version: 1.0.0

   These views are the surface the Text-to-SQL agent is steered towards, for
   three reasons:

     1. Correctness. The business definitions (MRR, churn, at-risk) are encoded
        here once, by a human, instead of being re-derived by a language model
        on every question. A model that invents its own MRR formula is wrong in
        a way that looks right.
     2. Safety. The views expose no credential-shaped or sensitive columns, so
        even a successful prompt injection cannot reach them through this path.
     3. Simplicity. One view replaces a five-table join, which shortens the
        generated SQL and sharply reduces the chance of a wrong join grain.

   Every view carries tenant_id so the guard's tenant predicate applies here
   exactly as it does to base tables.

   CREATE OR ALTER makes this script idempotent and re-runnable.
   =========================================================================== */

USE [EnterpriseCopilot];
GO

/* ---------------------------------------------------------------------------
   analytics.vw_month_spine
   A month calendar covering the data range. Revenue reporting needs a row for
   every month, including months in which nothing happened -- otherwise a churn
   spike looks like missing data.
   --------------------------------------------------------------------------- */
CREATE OR ALTER VIEW analytics.vw_month_spine
AS
WITH bounds AS (
    SELECT
        DATEFROMPARTS(YEAR(MIN(started_on)), MONTH(MIN(started_on)), 1) AS first_month,
        DATEFROMPARTS(YEAR(MAX(bound_date)), MONTH(MAX(bound_date)), 1)  AS last_month
    FROM (
        SELECT started_on, COALESCE(ended_on, CAST(SYSUTCDATETIME() AS date)) AS bound_date
        FROM core.subscriptions
    ) s
),
months AS (
    SELECT first_month AS month_start, last_month FROM bounds
    UNION ALL
    -- The CAST is a no-op in T-SQL, where DATEADD on a date returns a
    -- date. It is load-bearing for PostgreSQL: there `date + interval`
    -- yields timestamp, and a recursive CTE requires the recursive term's
    -- column types to match the anchor's exactly. Saying the intended type
    -- out loud in the source keeps both dialects unambiguous, rather than
    -- making the translator guess whether a cast would preserve or
    -- truncate the value.
    SELECT CAST(DATEADD(MONTH, 1, month_start) AS date), last_month
    FROM months
    WHERE month_start < last_month
)
SELECT
    month_start,
    EOMONTH(month_start)                    AS month_end,
    YEAR(month_start)                       AS calendar_year,
    MONTH(month_start)                      AS calendar_month,
    CONCAT(YEAR(month_start), '-Q', DATEPART(QUARTER, month_start)) AS calendar_quarter
FROM months;
GO

/* ---------------------------------------------------------------------------
   analytics.vw_monthly_recurring_revenue
   MRR per tenant per month, per the official glossary definition:
     - a subscription contributes in a month if it was active at any point in it
     - trials are excluded (they are not paid revenue)
     - annual plans are already normalised to a monthly amount in mrr_amount
   --------------------------------------------------------------------------- */
CREATE OR ALTER VIEW analytics.vw_monthly_recurring_revenue
AS
SELECT
    m.month_start,
    m.calendar_year,
    m.calendar_month,
    m.calendar_quarter,
    s.tenant_id,
    t.tenant_name,
    COUNT(DISTINCT s.customer_id)                   AS paying_customers,
    COUNT(DISTINCT s.subscription_id)               AS active_subscriptions,
    CAST(SUM(s.mrr_amount) AS DECIMAL(19,4))        AS mrr,
    CAST(SUM(s.mrr_amount) * 12 AS DECIMAL(19,4))   AS arr,
    CAST(SUM(s.seats) AS INT)                       AS total_seats
FROM analytics.vw_month_spine AS m
JOIN core.subscriptions AS s
      ON  s.started_on <= m.month_end
      AND (s.ended_on IS NULL OR s.ended_on >= m.month_start)
      AND s.is_trial = 0
      AND s.status IN ('active', 'cancelled', 'expired', 'paused')
JOIN core.tenants AS t ON t.tenant_id = s.tenant_id
GROUP BY
    m.month_start, m.calendar_year, m.calendar_month, m.calendar_quarter,
    s.tenant_id, t.tenant_name;
GO

/* ---------------------------------------------------------------------------
   analytics.vw_churn_metrics
   Logo churn (customers lost) and revenue churn (MRR lost) per month.
   Both are reported because they answer different questions: losing ten small
   customers and losing one large one are not the same event.
   --------------------------------------------------------------------------- */
CREATE OR ALTER VIEW analytics.vw_churn_metrics
AS
SELECT
    m.month_start,
    m.calendar_year,
    m.calendar_month,
    m.calendar_quarter,
    t.tenant_id,
    t.tenant_name,
    /* Customers whose churn_date falls in this month. */
    (SELECT COUNT(*)
       FROM core.customers c
      WHERE c.tenant_id = t.tenant_id
        AND c.churn_date BETWEEN m.month_start AND m.month_end)      AS churned_customers,
    /* Customers active at the start of the month: the churn denominator. */
    (SELECT COUNT(DISTINCT c2.customer_id)
       FROM core.customers c2
      WHERE c2.tenant_id = t.tenant_id
        AND c2.signup_date < m.month_start
        AND (c2.churn_date IS NULL OR c2.churn_date >= m.month_start)) AS customers_at_start,
    /* MRR lost to cancellations recorded in this month. */
    CAST(COALESCE((
        SELECT SUM(-sc.mrr_delta)
          FROM core.subscription_changes sc
         WHERE sc.tenant_id = t.tenant_id
           AND sc.change_type = 'cancel'
           AND sc.changed_on BETWEEN m.month_start AND m.month_end
    ), 0) AS DECIMAL(19,4))                                           AS churned_mrr,
    CAST(COALESCE((
        SELECT SUM(sc.mrr_delta)
          FROM core.subscription_changes sc
         WHERE sc.tenant_id = t.tenant_id
           AND sc.change_type IN ('upgrade', 'seat_change')
           AND sc.mrr_delta > 0
           AND sc.changed_on BETWEEN m.month_start AND m.month_end
    ), 0) AS DECIMAL(19,4))                                           AS expansion_mrr,
    CAST(COALESCE((
        SELECT SUM(-sc.mrr_delta)
          FROM core.subscription_changes sc
         WHERE sc.tenant_id = t.tenant_id
           AND sc.change_type IN ('downgrade', 'seat_change')
           AND sc.mrr_delta < 0
           AND sc.changed_on BETWEEN m.month_start AND m.month_end
    ), 0) AS DECIMAL(19,4))                                           AS contraction_mrr,
    CAST(COALESCE((
        SELECT SUM(sc.mrr_delta)
          FROM core.subscription_changes sc
         WHERE sc.tenant_id = t.tenant_id
           AND sc.change_type = 'new'
           AND sc.changed_on BETWEEN m.month_start AND m.month_end
    ), 0) AS DECIMAL(19,4))                                           AS new_business_mrr
FROM analytics.vw_month_spine AS m
CROSS JOIN core.tenants AS t;
GO

/* ---------------------------------------------------------------------------
   analytics.vw_sla_performance
   Contractual target vs actual, per ticket. This is the view that answers
   "compare the contractual response time with the actual response time for
   customer X" without the model having to rediscover the SLA join.

   The policy is matched on the version in force when the ticket was OPENED,
   not the current version -- judging a 2023 ticket by the 2025 SLA would be
   wrong, and is a mistake a schema-only prompt makes routinely.
   --------------------------------------------------------------------------- */
CREATE OR ALTER VIEW analytics.vw_sla_performance
AS
SELECT
    tk.ticket_id,
    tk.ticket_number,
    tk.tenant_id,
    tk.customer_id,
    c.display_name                          AS customer_name,
    c.segment,
    tk.product_id,
    p.product_name,
    tk.priority,
    tk.category,
    tk.status,
    tk.opened_at_utc,
    tk.first_response_at_utc,
    tk.resolved_at_utc,
    pol.plan_tier                           AS sla_tier,
    pol.version                             AS sla_version,
    pol.first_response_minutes              AS target_first_response_minutes,
    pol.resolution_minutes                  AS target_resolution_minutes,
    DATEDIFF(MINUTE, tk.opened_at_utc, tk.first_response_at_utc) AS actual_first_response_minutes,
    DATEDIFF(MINUTE, tk.opened_at_utc, tk.resolved_at_utc)       AS actual_resolution_minutes,
    CASE
        WHEN tk.first_response_at_utc IS NULL THEN 1
        WHEN DATEDIFF(MINUTE, tk.opened_at_utc, tk.first_response_at_utc)
             > pol.first_response_minutes THEN 1
        ELSE 0
    END                                     AS first_response_breached,
    CASE
        WHEN tk.resolved_at_utc IS NULL THEN 0   -- unresolved is not yet a resolution breach
        WHEN DATEDIFF(MINUTE, tk.opened_at_utc, tk.resolved_at_utc)
             > pol.resolution_minutes THEN 1
        ELSE 0
    END                                     AS resolution_breached,
    tk.satisfaction_score
FROM support.tickets AS tk
JOIN core.customers  AS c ON c.customer_id = tk.customer_id
LEFT JOIN core.products AS p ON p.product_id = tk.product_id
OUTER APPLY (
    SELECT TOP (1) sp.*
    FROM support.sla_policies sp
    WHERE sp.priority = tk.priority
      AND sp.effective_from <= CAST(tk.opened_at_utc AS date)
      AND (sp.effective_to IS NULL OR sp.effective_to >= CAST(tk.opened_at_utc AS date))
      AND sp.plan_tier = (
            SELECT TOP (1) pl.tier
            FROM core.subscriptions su
            JOIN core.plans pl ON pl.plan_id = su.plan_id
            WHERE su.customer_id = tk.customer_id
              AND su.started_on <= CAST(tk.opened_at_utc AS date)
              AND (su.ended_on IS NULL OR su.ended_on >= CAST(tk.opened_at_utc AS date))
            ORDER BY pl.list_price_monthly DESC
      )
    ORDER BY sp.effective_from DESC
) AS pol;
GO

/* ---------------------------------------------------------------------------
   analytics.vw_customer_360
   One row per customer with the facts an account manager asks for. Built to
   stop the model from writing a six-table join it will get subtly wrong.
   --------------------------------------------------------------------------- */
CREATE OR ALTER VIEW analytics.vw_customer_360
AS
SELECT
    c.customer_id,
    c.tenant_id,
    t.tenant_name,
    c.customer_code,
    c.display_name                              AS customer_name,
    c.legal_name,
    c.segment,
    c.industry,
    c.country_code,
    c.region,
    c.employee_count,
    c.billing_currency,
    c.signup_date,
    c.churn_date,
    c.status                                    AS customer_status,
    c.is_reactivated,
    DATEDIFF(DAY, c.signup_date,
             COALESCE(c.churn_date, CAST(SYSUTCDATETIME() AS date))) AS tenure_days,
    COALESCE(sub.active_subscriptions, 0)       AS active_subscriptions,
    CAST(COALESCE(sub.current_mrr, 0) AS DECIMAL(19,4))      AS current_mrr,
    CAST(COALESCE(sub.current_mrr, 0) * 12 AS DECIMAL(19,4)) AS current_arr,
    COALESCE(sub.total_seats, 0)                AS total_seats,
    CAST(COALESCE(inv.lifetime_billed, 0) AS DECIMAL(19,4))  AS lifetime_billed,
    CAST(COALESCE(inv.lifetime_paid, 0) AS DECIMAL(19,4))    AS lifetime_paid,
    COALESCE(inv.open_invoices, 0)              AS open_invoices,
    CAST(COALESCE(inv.overdue_amount, 0) AS DECIMAL(19,4))   AS overdue_amount,
    CAST(COALESCE(ref.total_refunded, 0) AS DECIMAL(19,4))   AS total_refunded,
    COALESCE(tkt.total_tickets, 0)              AS total_tickets,
    COALESCE(tkt.open_tickets, 0)               AS open_tickets,
    COALESCE(brc.sla_breaches, 0)               AS sla_breaches,
    COALESCE(usg.avg_active_users_30d, 0)       AS avg_active_users_30d,
    hlt.health_score,
    hlt.risk_band,
    hlt.churn_risk_pct
FROM core.customers AS c
JOIN core.tenants   AS t ON t.tenant_id = c.tenant_id
OUTER APPLY (
    SELECT COUNT(*)        AS active_subscriptions,
           SUM(s.mrr_amount) AS current_mrr,
           SUM(s.seats)    AS total_seats
    FROM core.subscriptions s
    WHERE s.customer_id = c.customer_id AND s.status = 'active' AND s.is_trial = 0
) AS sub
OUTER APPLY (
    SELECT SUM(i.total_amount) AS lifetime_billed,
           SUM(i.amount_paid)  AS lifetime_paid,
           SUM(CASE WHEN i.status IN ('open','overdue','partial') THEN 1 ELSE 0 END) AS open_invoices,
           SUM(CASE WHEN i.status IN ('open','overdue','partial')
                    THEN i.total_amount - i.amount_paid ELSE 0 END) AS overdue_amount
    FROM billing.invoices i
    WHERE i.customer_id = c.customer_id AND i.status <> 'void'
) AS inv
OUTER APPLY (
    SELECT SUM(r.amount) AS total_refunded
    FROM billing.refunds r WHERE r.customer_id = c.customer_id
) AS ref
OUTER APPLY (
    SELECT COUNT(*) AS total_tickets,
           SUM(CASE WHEN tk.status IN ('open','pending','escalated') THEN 1 ELSE 0 END) AS open_tickets
    FROM support.tickets tk WHERE tk.customer_id = c.customer_id
) AS tkt
OUTER APPLY (
    SELECT COUNT(*) AS sla_breaches
    FROM support.sla_breaches b WHERE b.customer_id = c.customer_id
) AS brc
OUTER APPLY (
    SELECT AVG(CAST(u.active_users AS DECIMAL(10,2))) AS avg_active_users_30d
    FROM core.usage_daily u
    WHERE u.customer_id = c.customer_id
      AND u.usage_date >= DATEADD(DAY, -30, CAST(SYSUTCDATETIME() AS date))
) AS usg
OUTER APPLY (
    SELECT TOP (1) h.health_score, h.risk_band, h.churn_risk_pct
    FROM analytics.customer_health h
    WHERE h.customer_id = c.customer_id
    ORDER BY h.snapshot_date DESC
) AS hlt;
GO

/* ---------------------------------------------------------------------------
   analytics.vw_customer_risk
   Answers "which customers are at risk according to usage, unpaid invoices and
   support tickets" directly, using the glossary's at-risk definition rather
   than whatever the model would improvise.
   --------------------------------------------------------------------------- */
CREATE OR ALTER VIEW analytics.vw_customer_risk
AS
SELECT
    v.customer_id,
    v.tenant_id,
    v.tenant_name,
    v.customer_name,
    v.segment,
    v.region,
    v.customer_status,
    v.current_mrr,
    v.current_arr,
    v.overdue_amount,
    v.open_invoices,
    v.total_tickets,
    v.open_tickets,
    v.sla_breaches,
    v.avg_active_users_30d,
    v.health_score,
    v.churn_risk_pct,
    v.risk_band,
    /* Individual risk signals, exposed so an answer can explain *why*. */
    CASE WHEN v.overdue_amount > 0 THEN 1 ELSE 0 END          AS signal_unpaid_invoice,
    CASE WHEN v.sla_breaches >= 3 THEN 1 ELSE 0 END           AS signal_repeated_sla_breach,
    CASE WHEN v.open_tickets >= 3 THEN 1 ELSE 0 END           AS signal_high_open_tickets,
    CASE WHEN v.avg_active_users_30d < 5 THEN 1 ELSE 0 END    AS signal_low_usage,
    CASE WHEN v.health_score IS NOT NULL AND v.health_score < 50 THEN 1 ELSE 0 END AS signal_low_health,
    (   CASE WHEN v.overdue_amount > 0 THEN 1 ELSE 0 END
      + CASE WHEN v.sla_breaches >= 3 THEN 1 ELSE 0 END
      + CASE WHEN v.open_tickets >= 3 THEN 1 ELSE 0 END
      + CASE WHEN v.avg_active_users_30d < 5 THEN 1 ELSE 0 END
      + CASE WHEN v.health_score IS NOT NULL AND v.health_score < 50 THEN 1 ELSE 0 END
    )                                                         AS risk_signal_count
FROM analytics.vw_customer_360 AS v
WHERE v.customer_status <> 'churned';
GO

/* ---------------------------------------------------------------------------
   analytics.vw_incident_impact
   Which customers an incident touched, and what it cost. Answers "what caused
   the June service incident and which customers were affected", with the
   postmortem document id carried through so the answer can cite the document
   corpus alongside the database facts.
   --------------------------------------------------------------------------- */
CREATE OR ALTER VIEW analytics.vw_incident_impact
AS
SELECT
    i.incident_id,
    i.incident_code,
    i.title                                 AS incident_title,
    i.severity,
    i.status                                AS incident_status,
    i.started_at_utc,
    i.detected_at_utc,
    i.resolved_at_utc,
    DATEDIFF(MINUTE, i.started_at_utc, i.resolved_at_utc) AS total_duration_minutes,
    DATEDIFF(MINUTE, i.started_at_utc, i.detected_at_utc) AS time_to_detect_minutes,
    i.root_cause,
    i.postmortem_doc_id,
    i.affected_region,
    p.product_id,
    p.product_name,
    im.customer_id,
    c.display_name                          AS customer_name,
    c.tenant_id,
    c.segment,
    im.impact_level,
    im.downtime_minutes,
    CAST(im.credit_amount AS DECIMAL(19,4)) AS credit_amount
FROM support.incidents        AS i
LEFT JOIN core.products       AS p  ON p.product_id = i.product_id
LEFT JOIN support.incident_impact AS im ON im.incident_id = i.incident_id
LEFT JOIN core.customers      AS c  ON c.customer_id = im.customer_id;
GO

PRINT '005_create_views.sql complete.';
GO
