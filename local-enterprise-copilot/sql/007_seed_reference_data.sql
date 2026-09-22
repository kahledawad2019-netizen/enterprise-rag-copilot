/* ===========================================================================
   007_seed_reference_data.sql
   Schema version: 1.0.0

   Reference data for "Northwind Cloud", a fictional B2B SaaS company.

   This script seeds everything that is a *definition* rather than a
   *transaction*: tenants, products, plans, SLA policies, access groups, the
   business glossary, and the approved Text-to-SQL examples. Transactional
   volume (customers, subscriptions, invoices, tickets) is produced separately
   by scripts/generate_synthetic_data.py from a fixed seed.

   Splitting it this way matters: reference data is reviewed by humans and
   version-controlled here, while transactional data is regenerated freely.

   Idempotent: every insert is guarded by NOT EXISTS on a natural key.
   =========================================================================== */

USE [EnterpriseCopilot];
GO

/* ---------------------------------------------------------------------------
   Tenants - three business units, deliberately in different regions and
   currencies so cross-tenant isolation and multi-currency are both testable.
   --------------------------------------------------------------------------- */
INSERT INTO core.tenants (tenant_code, tenant_name, region, default_currency, time_zone)
SELECT v.tenant_code, v.tenant_name, v.region, v.default_currency, v.time_zone
FROM (VALUES
    ('NWC-NA',  N'Northwind Cloud North America', 'North America', 'USD', 'America/New_York'),
    ('NWC-EU',  N'Northwind Cloud EMEA',          'EMEA',          'EUR', 'Europe/Berlin'),
    ('NWC-APAC',N'Northwind Cloud APAC',          'APAC',          'USD', 'Asia/Singapore')
) AS v(tenant_code, tenant_name, region, default_currency, time_zone)
WHERE NOT EXISTS (SELECT 1 FROM core.tenants t WHERE t.tenant_code = v.tenant_code);
GO

/* ---------------------------------------------------------------------------
   Products - five SaaS products across three families.
   --------------------------------------------------------------------------- */
INSERT INTO core.products (product_code, product_name, product_family, description, launched_on)
SELECT v.product_code, v.product_name, v.product_family, v.description, v.launched_on
FROM (VALUES
    ('NW-ANALYTICS', N'Northwind Analytics',  'Data',
     N'Self-service BI and dashboards for operational teams.',            '2021-03-01'),
    ('NW-PIPELINE',  N'Northwind DataPipeline','Data',
     N'Managed ETL and streaming ingestion with 200+ connectors.',        '2021-09-15'),
    ('NW-DESK',      N'Northwind ServiceDesk', 'Service',
     N'Ticketing, SLA management and customer support workflows.',        '2020-06-01'),
    ('NW-SHIELD',    N'Northwind Shield',      'Security',
     N'Audit logging, access governance and compliance reporting.',       '2022-01-10'),
    ('NW-CONNECT',   N'Northwind Connect',     'Service',
     N'Embedded messaging and customer notification platform.',           '2022-11-01')
) AS v(product_code, product_name, product_family, description, launched_on)
WHERE NOT EXISTS (SELECT 1 FROM core.products p WHERE p.product_code = v.product_code);
GO

/* ---------------------------------------------------------------------------
   Plans - three tiers per product, monthly and annual.

   list_price_monthly is always a MONTHLY figure, including for annual plans.
   Annual plans are cheaper per month: that discount is the reason the MRR
   glossary entry has to say how annual contracts are normalised, and it is a
   calculation a schema-only prompt gets wrong roughly every time.
   --------------------------------------------------------------------------- */
INSERT INTO core.plans
    (product_id, plan_code, plan_name, tier, billing_interval,
     list_price_monthly, currency_code, seats_included, effective_from)
SELECT p.product_id, v.plan_code, v.plan_name, v.tier, v.billing_interval,
       v.list_price_monthly, 'USD', v.seats_included, v.effective_from
FROM (VALUES
    -- Analytics
    ('NW-ANALYTICS','ANL-START-M', N'Analytics Starter (Monthly)',      'Starter',     'monthly',   99.0000,  5, '2021-03-01'),
    ('NW-ANALYTICS','ANL-START-A', N'Analytics Starter (Annual)',       'Starter',     'annual',    82.5000,  5, '2021-03-01'),
    ('NW-ANALYTICS','ANL-PRO-M',   N'Analytics Professional (Monthly)', 'Professional','monthly',  499.0000, 25, '2021-03-01'),
    ('NW-ANALYTICS','ANL-PRO-A',   N'Analytics Professional (Annual)',  'Professional','annual',   415.0000, 25, '2021-03-01'),
    ('NW-ANALYTICS','ANL-ENT-M',   N'Analytics Enterprise (Monthly)',   'Enterprise',  'monthly', 1999.0000,100, '2021-03-01'),
    ('NW-ANALYTICS','ANL-ENT-A',   N'Analytics Enterprise (Annual)',    'Enterprise',  'annual',  1665.0000,100, '2021-03-01'),
    -- DataPipeline
    ('NW-PIPELINE', 'PIP-START-M', N'Pipeline Starter (Monthly)',       'Starter',     'monthly',  149.0000,  3, '2021-09-15'),
    ('NW-PIPELINE', 'PIP-PRO-M',   N'Pipeline Professional (Monthly)',  'Professional','monthly',  749.0000, 15, '2021-09-15'),
    ('NW-PIPELINE', 'PIP-PRO-A',   N'Pipeline Professional (Annual)',   'Professional','annual',   624.0000, 15, '2021-09-15'),
    ('NW-PIPELINE', 'PIP-ENT-A',   N'Pipeline Enterprise (Annual)',     'Enterprise',  'annual',  2499.0000, 60, '2021-09-15'),
    -- ServiceDesk
    ('NW-DESK',     'DSK-START-M', N'ServiceDesk Starter (Monthly)',    'Starter',     'monthly',   79.0000, 10, '2020-06-01'),
    ('NW-DESK',     'DSK-PRO-M',   N'ServiceDesk Professional (Monthly)','Professional','monthly',  389.0000, 40, '2020-06-01'),
    ('NW-DESK',     'DSK-PRO-A',   N'ServiceDesk Professional (Annual)','Professional','annual',   324.0000, 40, '2020-06-01'),
    ('NW-DESK',     'DSK-ENT-A',   N'ServiceDesk Enterprise (Annual)',  'Enterprise',  'annual',  1499.0000,150, '2020-06-01'),
    -- Shield
    ('NW-SHIELD',   'SHD-PRO-M',   N'Shield Professional (Monthly)',    'Professional','monthly',  599.0000, 20, '2022-01-10'),
    ('NW-SHIELD',   'SHD-ENT-M',   N'Shield Enterprise (Monthly)',      'Enterprise',  'monthly', 2299.0000, 80, '2022-01-10'),
    ('NW-SHIELD',   'SHD-ENT-A',   N'Shield Enterprise (Annual)',       'Enterprise',  'annual',  1915.0000, 80, '2022-01-10'),
    -- Connect
    ('NW-CONNECT',  'CON-START-M', N'Connect Starter (Monthly)',        'Starter',     'monthly',   59.0000,  5, '2022-11-01'),
    ('NW-CONNECT',  'CON-PRO-M',   N'Connect Professional (Monthly)',   'Professional','monthly',  299.0000, 25, '2022-11-01'),
    ('NW-CONNECT',  'CON-ENT-A',   N'Connect Enterprise (Annual)',      'Enterprise',  'annual',  1249.0000, 75, '2022-11-01')
) AS v(product_code, plan_code, plan_name, tier, billing_interval,
       list_price_monthly, seats_included, effective_from)
JOIN core.products p ON p.product_code = v.product_code
WHERE NOT EXISTS (SELECT 1 FROM core.plans pl WHERE pl.plan_code = v.plan_code);
GO

/* ---------------------------------------------------------------------------
   SLA policies - VERSIONED.

   v1.0 ran to 2024-12-31; v2.0 tightened Enterprise P1 first response from
   30 to 15 minutes. Keeping both means a 2023 ticket is judged by the 2023
   contract. This is the structured mirror of the document corpus's superseded
   SLA policy, and it is what the version-sensitive evaluation questions test.
   --------------------------------------------------------------------------- */
INSERT INTO support.sla_policies
    (policy_code, plan_tier, priority, first_response_minutes, resolution_minutes,
     coverage_hours, uptime_target_pct, version, effective_from, effective_to, is_current)
SELECT v.policy_code, v.plan_tier, v.priority, v.first_response_minutes, v.resolution_minutes,
       v.coverage_hours, v.uptime_target_pct, v.version, v.effective_from, v.effective_to, v.is_current
FROM (VALUES
    -- ---- v1.0 : in force 2020-06-01 .. 2024-12-31 ----
    ('SLA-ENT-P1','Enterprise',  'P1',   30,   240,'24x7',           99.90,'1.0','2020-06-01','2024-12-31',0),
    ('SLA-ENT-P2','Enterprise',  'P2',   60,   480,'24x7',           99.90,'1.0','2020-06-01','2024-12-31',0),
    ('SLA-ENT-P3','Enterprise',  'P3',  240,  2880,'business_hours', 99.90,'1.0','2020-06-01','2024-12-31',0),
    ('SLA-ENT-P4','Enterprise',  'P4',  480,  7200,'business_hours', 99.90,'1.0','2020-06-01','2024-12-31',0),
    ('SLA-PRO-P1','Professional','P1',   60,   480,'24x7',           99.50,'1.0','2020-06-01','2024-12-31',0),
    ('SLA-PRO-P2','Professional','P2',  120,   960,'business_hours', 99.50,'1.0','2020-06-01','2024-12-31',0),
    ('SLA-PRO-P3','Professional','P3',  480,  4320,'business_hours', 99.50,'1.0','2020-06-01','2024-12-31',0),
    ('SLA-PRO-P4','Professional','P4',  960, 10080,'business_hours', 99.50,'1.0','2020-06-01','2024-12-31',0),
    ('SLA-STR-P1','Starter',     'P1',  240,  1440,'business_hours', 99.00,'1.0','2020-06-01','2024-12-31',0),
    ('SLA-STR-P2','Starter',     'P2',  480,  2880,'business_hours', 99.00,'1.0','2020-06-01','2024-12-31',0),
    ('SLA-STR-P3','Starter',     'P3',  960,  7200,'business_hours', 99.00,'1.0','2020-06-01','2024-12-31',0),
    ('SLA-STR-P4','Starter',     'P4', 1440, 14400,'business_hours', 99.00,'1.0','2020-06-01','2024-12-31',0),
    -- ---- v2.0 : in force from 2025-01-01 (Enterprise P1 tightened to 15 min) ----
    ('SLA-ENT-P1','Enterprise',  'P1',   15,   180,'24x7',           99.95,'2.0','2025-01-01',NULL,1),
    ('SLA-ENT-P2','Enterprise',  'P2',   45,   360,'24x7',           99.95,'2.0','2025-01-01',NULL,1),
    ('SLA-ENT-P3','Enterprise',  'P3',  180,  2160,'business_hours', 99.95,'2.0','2025-01-01',NULL,1),
    ('SLA-ENT-P4','Enterprise',  'P4',  480,  7200,'business_hours', 99.95,'2.0','2025-01-01',NULL,1),
    ('SLA-PRO-P1','Professional','P1',   45,   360,'24x7',           99.50,'2.0','2025-01-01',NULL,1),
    ('SLA-PRO-P2','Professional','P2',  120,   720,'business_hours', 99.50,'2.0','2025-01-01',NULL,1),
    ('SLA-PRO-P3','Professional','P3',  480,  4320,'business_hours', 99.50,'2.0','2025-01-01',NULL,1),
    ('SLA-PRO-P4','Professional','P4',  960, 10080,'business_hours', 99.50,'2.0','2025-01-01',NULL,1),
    ('SLA-STR-P1','Starter',     'P1',  240,  1440,'business_hours', 99.00,'2.0','2025-01-01',NULL,1),
    ('SLA-STR-P2','Starter',     'P2',  480,  2880,'business_hours', 99.00,'2.0','2025-01-01',NULL,1),
    ('SLA-STR-P3','Starter',     'P3',  960,  7200,'business_hours', 99.00,'2.0','2025-01-01',NULL,1),
    ('SLA-STR-P4','Starter',     'P4', 1440, 14400,'business_hours', 99.00,'2.0','2025-01-01',NULL,1)
) AS v(policy_code, plan_tier, priority, first_response_minutes, resolution_minutes,
       coverage_hours, uptime_target_pct, version, effective_from, effective_to, is_current)
WHERE NOT EXISTS (
    SELECT 1 FROM support.sla_policies sp
    WHERE sp.policy_code = v.policy_code AND sp.version = v.version
);
GO

/* ---------------------------------------------------------------------------
   Access groups - document-level permissions for the RAG layer.
   --------------------------------------------------------------------------- */
INSERT INTO security.access_groups (group_code, group_name, description)
SELECT v.group_code, v.group_name, v.description
FROM (VALUES
    ('public',        N'Public',          N'Readable by every authenticated user.'),
    ('internal',      N'Internal',        N'Northwind Cloud employees only.'),
    ('finance',       N'Finance',         N'Pricing, discounting and revenue policy.'),
    ('support',       N'Support',         N'Escalation runbooks and SLA operations.'),
    ('security',      N'Security',        N'Security policy and incident response.'),
    ('exec',          N'Executive',       N'Board-level and strategic material.')
) AS v(group_code, group_name, description)
WHERE NOT EXISTS (SELECT 1 FROM security.access_groups g WHERE g.group_code = v.group_code);
GO

/* ---------------------------------------------------------------------------
   Application users - drives tenant isolation and permission tests.
   `analyst_eu` exists specifically so a cross-tenant request can be attempted
   and proven to fail.
   --------------------------------------------------------------------------- */
INSERT INTO security.app_users (user_name, display_name, tenant_id, access_groups, is_admin)
SELECT v.user_name, v.display_name, t.tenant_id, v.access_groups, v.is_admin
FROM (VALUES
    ('admin',      N'Platform Administrator', 'NWC-NA',  'public,internal,finance,support,security,exec', 1),
    ('analyst_na', N'Revenue Analyst (NA)',   'NWC-NA',  'public,internal,finance',                       0),
    ('analyst_eu', N'Revenue Analyst (EMEA)', 'NWC-EU',  'public,internal,finance',                       0),
    ('support_na', N'Support Lead (NA)',      'NWC-NA',  'public,internal,support',                       0),
    ('guest',      N'Guest (public only)',    'NWC-NA',  'public',                                        0)
) AS v(user_name, display_name, tenant_code, access_groups, is_admin)
JOIN core.tenants t ON t.tenant_code = v.tenant_code
WHERE NOT EXISTS (SELECT 1 FROM security.app_users u WHERE u.user_name = v.user_name);
GO

PRINT '007: tenants, products, plans, SLA policies, access groups and users seeded.';
GO

/* ===========================================================================
   BUSINESS GLOSSARY

   This is the semantic layer, and it is the single most important reason this
   system answers business questions correctly.

   The schema says `subscriptions.mrr_amount DECIMAL(19,4)`. It does not say
   that trials are excluded, that annual contracts are already normalised, or
   that a paused subscription still counts. A model given only the schema will
   invent a plausible and wrong definition. Every entry below therefore carries
   SQL guidance and explicit exclusions, and both are injected into the
   Text-to-SQL prompt.
   =========================================================================== */
INSERT INTO ai.business_glossary
    (term, definition, sql_guidance, owner, version, effective_date,
     related_tables, related_columns, example_calculation, known_exclusions, is_current)
SELECT v.term, v.definition, v.sql_guidance, v.owner, v.version, v.effective_date,
       v.related_tables, v.related_columns, v.example_calculation, v.known_exclusions, 1
FROM (VALUES
(N'MRR',
 N'Monthly Recurring Revenue. The normalised monthly value of all paid, non-trial subscriptions that were active at any point during the month. Annual contracts are divided across the twelve months they cover rather than recognised in the month they are billed.',
 N'Use analytics.vw_monthly_recurring_revenue. If querying base tables, sum core.subscriptions.mrr_amount where is_trial = 0 and started_on <= end of month and (ended_on IS NULL OR ended_on >= start of month). Never sum invoice totals to get MRR: a single annual invoice would inflate one month twelvefold.',
 N'Finance', '2.0', '2025-01-01',
 N'core.subscriptions, analytics.vw_monthly_recurring_revenue',
 N'subscriptions.mrr_amount, subscriptions.is_trial, subscriptions.started_on, subscriptions.ended_on',
 N'SELECT month_start, SUM(mrr) FROM analytics.vw_monthly_recurring_revenue GROUP BY month_start;',
 N'Excludes trials, one-off professional-services fees, usage overage charges, and taxes.'),

(N'ARR',
 N'Annual Recurring Revenue. MRR multiplied by twelve. A forward-looking run rate, not billed or collected revenue.',
 N'ARR = MRR * 12. Use analytics.vw_monthly_recurring_revenue.arr, or vw_customer_360.current_arr for a single customer. Do not compute ARR by summing twelve months of invoices; that is trailing revenue, which is a different number.',
 N'Finance', '2.0', '2025-01-01',
 N'core.subscriptions, analytics.vw_monthly_recurring_revenue, analytics.vw_customer_360',
 N'subscriptions.mrr_amount, vw_customer_360.current_arr',
 N'SELECT TOP 5 customer_name, current_arr FROM analytics.vw_customer_360 ORDER BY current_arr DESC;',
 N'Same exclusions as MRR. ARR is never reduced for expected churn.'),

(N'Active customer',
 N'A customer with at least one active, non-trial subscription and no churn date. Status alone is not sufficient: a customer can be flagged active while every subscription has lapsed.',
 N'Prefer analytics.vw_customer_360 WHERE customer_status = ''active'' AND active_subscriptions > 0. On base tables, join core.customers to core.subscriptions with s.status = ''active'' AND s.is_trial = 0 AND c.churn_date IS NULL.',
 N'Revenue Operations', '1.1', '2024-06-01',
 N'core.customers, core.subscriptions, analytics.vw_customer_360',
 N'customers.status, customers.churn_date, subscriptions.status, subscriptions.is_trial',
 N'SELECT COUNT(*) FROM analytics.vw_customer_360 WHERE customer_status = ''active'' AND active_subscriptions > 0;',
 N'Excludes trial-only customers, suspended accounts, and customers whose only subscription is paused.'),

(N'Active subscription',
 N'A subscription with status ''active'', not a trial, whose start date has passed and whose end date is either null or in the future.',
 N'core.subscriptions WHERE status = ''active'' AND is_trial = 0 AND started_on <= CAST(GETDATE() AS date) AND (ended_on IS NULL OR ended_on >= CAST(GETDATE() AS date)).',
 N'Revenue Operations', '1.0', '2023-01-01',
 N'core.subscriptions', N'subscriptions.status, subscriptions.is_trial, subscriptions.started_on, subscriptions.ended_on',
 N'SELECT COUNT(*) FROM core.subscriptions WHERE status = ''active'' AND is_trial = 0;',
 N'Paused subscriptions are excluded from this count but still contribute to MRR.'),

(N'New business',
 N'MRR from customers acquired in the period who had no prior paid subscription. Distinct from expansion, which comes from existing customers.',
 N'Sum core.subscription_changes.mrr_delta WHERE change_type = ''new'' in the period. Use analytics.vw_churn_metrics.new_business_mrr.',
 N'Finance', '1.0', '2023-01-01',
 N'core.subscription_changes, analytics.vw_churn_metrics', N'subscription_changes.change_type, subscription_changes.mrr_delta',
 N'SELECT month_start, new_business_mrr FROM analytics.vw_churn_metrics ORDER BY month_start;',
 N'Excludes reactivated customers; those are counted separately as reactivation.'),

(N'Expansion revenue',
 N'Additional MRR from existing customers through upgrades or seat increases within the period.',
 N'Sum positive mrr_delta from core.subscription_changes WHERE change_type IN (''upgrade'',''seat_change'') AND mrr_delta > 0. Use analytics.vw_churn_metrics.expansion_mrr.',
 N'Finance', '1.0', '2023-01-01',
 N'core.subscription_changes, analytics.vw_churn_metrics', N'subscription_changes.change_type, subscription_changes.mrr_delta',
 N'SELECT SUM(expansion_mrr) FROM analytics.vw_churn_metrics WHERE calendar_year = 2025;',
 N'Excludes new customers and one-off overage charges.'),

(N'Contraction revenue',
 N'MRR lost from existing customers who downgraded or reduced seats but did not cancel. Reported as a positive number representing the amount lost.',
 N'Sum the absolute value of negative mrr_delta WHERE change_type IN (''downgrade'',''seat_change''). Use analytics.vw_churn_metrics.contraction_mrr.',
 N'Finance', '1.0', '2023-01-01',
 N'core.subscription_changes, analytics.vw_churn_metrics', N'subscription_changes.change_type, subscription_changes.mrr_delta',
 N'SELECT SUM(contraction_mrr) FROM analytics.vw_churn_metrics WHERE calendar_year = 2025;',
 N'Excludes full cancellations, which are revenue churn, not contraction.'),

(N'Churned customer',
 N'A customer who has cancelled every paid subscription and has a non-null churn_date. Churn is recognised on the subscription end date, not on the date notice was given.',
 N'core.customers WHERE status = ''churned'' AND churn_date IS NOT NULL. For a period, filter churn_date BETWEEN the period bounds.',
 N'Revenue Operations', '1.1', '2024-06-01',
 N'core.customers, analytics.vw_churn_metrics', N'customers.status, customers.churn_date',
 N'SELECT COUNT(*) FROM core.customers WHERE churn_date BETWEEN ''2025-04-01'' AND ''2025-06-30'';',
 N'Excludes trial expiries, which were never paid customers, and excludes customers who downgraded but stayed.'),

(N'Revenue churn',
 N'MRR lost to cancellations in a period, divided by MRR at the start of the period. Also called gross MRR churn.',
 N'Use analytics.vw_churn_metrics.churned_mrr over the prior month''s mrr from vw_monthly_recurring_revenue. Do not net expansion against it; that would be net revenue retention, a different metric.',
 N'Finance', '1.0', '2023-01-01',
 N'analytics.vw_churn_metrics, analytics.vw_monthly_recurring_revenue', N'churn_metrics.churned_mrr',
 N'churned_mrr / NULLIF(previous_month_mrr, 0)',
 N'Excludes contraction. Gross churn never nets off expansion revenue.'),

(N'Logo churn',
 N'The count of customers lost in a period, divided by the count active at the start. Treats every customer equally regardless of size.',
 N'Use analytics.vw_churn_metrics: churned_customers / NULLIF(customers_at_start, 0).',
 N'Revenue Operations', '1.0', '2023-01-01',
 N'core.customers, analytics.vw_churn_metrics', N'churn_metrics.churned_customers, churn_metrics.customers_at_start',
 N'SELECT month_start, CAST(churned_customers AS float) / NULLIF(customers_at_start,0) FROM analytics.vw_churn_metrics;',
 N'Excludes trial-only customers. Logo churn and revenue churn can move in opposite directions.'),

(N'Trial customer',
 N'A customer whose only subscriptions have is_trial = 1. Trials are excluded from all revenue metrics.',
 N'core.subscriptions WHERE is_trial = 1. A customer is trial-only when no subscription has is_trial = 0.',
 N'Revenue Operations', '1.0', '2023-01-01',
 N'core.subscriptions, core.customers', N'subscriptions.is_trial, customers.status',
 N'SELECT COUNT(DISTINCT customer_id) FROM core.subscriptions WHERE is_trial = 1;',
 N'Never included in MRR, ARR, or churn denominators.'),

(N'Paid customer',
 N'A customer with at least one non-trial subscription that has generated at least one non-void invoice.',
 N'Join core.customers to core.subscriptions (is_trial = 0) and billing.invoices (status <> ''void'').',
 N'Finance', '1.0', '2023-01-01',
 N'core.customers, core.subscriptions, billing.invoices', N'subscriptions.is_trial, invoices.status',
 N'SELECT COUNT(DISTINCT c.customer_id) FROM core.customers c JOIN core.subscriptions s ON s.customer_id = c.customer_id AND s.is_trial = 0;',
 N'Excludes trial-only customers and customers whose invoices were all voided.'),

(N'Overdue invoice',
 N'An invoice past its due date that has not been paid in full. Partially paid invoices remain overdue for the unpaid balance.',
 N'billing.invoices WHERE status IN (''open'',''overdue'',''partial'') AND due_date < CAST(GETDATE() AS date). The outstanding amount is total_amount - amount_paid, not total_amount.',
 N'Finance', '1.1', '2024-09-01',
 N'billing.invoices, analytics.vw_customer_360', N'invoices.status, invoices.due_date, invoices.total_amount, invoices.amount_paid',
 N'SELECT SUM(total_amount - amount_paid) FROM billing.invoices WHERE status IN (''open'',''overdue'',''partial'') AND due_date < CAST(GETDATE() AS date);',
 N'Excludes voided and draft invoices. An invoice due today is not yet overdue.'),

(N'Refund rate',
 N'Total refunded amount divided by total billed amount over the same period, expressed as a percentage.',
 N'SUM(billing.refunds.amount) / NULLIF(SUM(billing.invoices.total_amount), 0). Align both to the same period and exclude voided invoices.',
 N'Finance', '1.0', '2023-01-01',
 N'billing.refunds, billing.invoices', N'refunds.amount, refunds.refund_date, invoices.total_amount, invoices.issue_date',
 N'SELECT SUM(r.amount) / NULLIF((SELECT SUM(total_amount) FROM billing.invoices WHERE status <> ''void''), 0) FROM billing.refunds r;',
 N'Excludes SLA service credits issued as incident compensation, which are tracked in support.incident_impact.credit_amount.'),

(N'SLA breach',
 N'A ticket whose first response or resolution exceeded the contractual target in the SLA version that was in force ON THE DATE THE TICKET WAS OPENED. A ticket that never received a first response is always a first-response breach.',
 N'Use analytics.vw_sla_performance, which already resolves the correct SLA version by ticket open date. Do not join support.sla_policies on is_current = 1: that judges historical tickets against today''s contract and silently misreports every pre-2025 ticket.',
 N'Support Operations', '2.0', '2025-01-01',
 N'support.tickets, support.sla_policies, support.sla_breaches, analytics.vw_sla_performance',
 N'tickets.opened_at_utc, tickets.first_response_at_utc, sla_policies.first_response_minutes, sla_policies.effective_from',
 N'SELECT customer_name, COUNT(*) FROM analytics.vw_sla_performance WHERE first_response_breached = 1 GROUP BY customer_name;',
 N'Excludes tickets opened outside coverage hours for business_hours policies, and tickets the customer closed before any response was due.'),

(N'First-response time',
 N'Elapsed minutes between ticket open and the first agent response. Measured in UTC.',
 N'DATEDIFF(MINUTE, tickets.opened_at_utc, tickets.first_response_at_utc). NULL first_response_at_utc means no response was ever given; treat that as a breach, not as zero.',
 N'Support Operations', '1.0', '2023-01-01',
 N'support.tickets, analytics.vw_sla_performance', N'tickets.opened_at_utc, tickets.first_response_at_utc',
 N'SELECT AVG(CAST(actual_first_response_minutes AS float)) FROM analytics.vw_sla_performance WHERE actual_first_response_minutes IS NOT NULL;',
 N'Excludes automated acknowledgements. Only a human agent response stops the clock.'),

(N'Resolution time',
 N'Elapsed minutes between ticket open and resolution. A ticket that is closed without being resolved has no resolution time.',
 N'DATEDIFF(MINUTE, tickets.opened_at_utc, tickets.resolved_at_utc). Exclude rows where resolved_at_utc IS NULL rather than treating them as zero.',
 N'Support Operations', '1.0', '2023-01-01',
 N'support.tickets, analytics.vw_sla_performance', N'tickets.opened_at_utc, tickets.resolved_at_utc',
 N'SELECT AVG(CAST(actual_resolution_minutes AS float)) FROM analytics.vw_sla_performance WHERE actual_resolution_minutes IS NOT NULL;',
 N'Excludes time spent in ''pending customer'' status for contractual purposes, though the stored value does not subtract it.'),

(N'At-risk customer',
 N'An active customer showing two or more of: an overdue invoice, three or more SLA breaches, three or more open tickets, fewer than five average active users over the last thirty days, or a health score below fifty.',
 N'Use analytics.vw_customer_risk WHERE risk_signal_count >= 2. The individual signal_* columns explain which factors triggered it, so an answer can justify itself rather than asserting risk.',
 N'Customer Success', '1.2', '2025-03-01',
 N'analytics.vw_customer_risk, analytics.customer_health',
 N'vw_customer_risk.risk_signal_count, signal_unpaid_invoice, signal_repeated_sla_breach, signal_low_usage',
 N'SELECT customer_name, risk_signal_count FROM analytics.vw_customer_risk WHERE risk_signal_count >= 2 ORDER BY current_arr DESC;',
 N'Excludes already-churned customers and customers in their first 30 days, whose usage has not ramped.'),

(N'Product adoption',
 N'The share of purchased seats that were actually used in the last 30 days, per customer and product.',
 N'avg(core.usage_daily.active_users) over the last 30 days divided by core.subscriptions.seats for the matching product. Guard against division by zero with NULLIF.',
 N'Customer Success', '1.0', '2024-01-01',
 N'core.usage_daily, core.subscriptions', N'usage_daily.active_users, usage_daily.usage_date, subscriptions.seats',
 N'SELECT customer_id, AVG(CAST(active_users AS float)) FROM core.usage_daily WHERE usage_date >= DATEADD(day,-30,CAST(GETDATE() AS date)) GROUP BY customer_id;',
 N'Excludes service accounts and API-only integrations, which do not register as active users.')
) AS v(term, definition, sql_guidance, owner, version, effective_date,
       related_tables, related_columns, example_calculation, known_exclusions)
WHERE NOT EXISTS (
    SELECT 1 FROM ai.business_glossary g WHERE g.term = v.term AND g.version = v.version
);
GO

/* A superseded definition, kept to prove version filtering works.
   MRR v1.0 wrongly included trials; v2.0 excluded them. A question about the
   *current* definition must not retrieve this row. */
INSERT INTO ai.business_glossary
    (term, definition, sql_guidance, owner, version, effective_date,
     related_tables, related_columns, example_calculation, known_exclusions, is_current)
SELECT N'MRR',
       N'SUPERSEDED (v1.0, in force 2023-01-01 to 2024-12-31). Monthly Recurring Revenue was previously defined as the normalised monthly value of ALL subscriptions including trials.',
       N'Historical only. Do not use for current reporting. Retained so that restatements of 2023-2024 figures can be reproduced.',
       N'Finance', '1.0', '2023-01-01',
       N'core.subscriptions', N'subscriptions.mrr_amount',
       N'Superseded by v2.0 on 2025-01-01.',
       N'This version did NOT exclude trials, which overstated MRR by roughly 4 percent.',
       0
WHERE NOT EXISTS (
    SELECT 1 FROM ai.business_glossary g WHERE g.term = N'MRR' AND g.version = '1.0'
);
GO

PRINT '007: business glossary seeded.';
GO

/* ===========================================================================
   APPROVED SQL EXAMPLES

   Few-shot examples are the highest-leverage input to Text-to-SQL accuracy --
   higher than schema detail, higher than prompt wording. Each is human
   verified. They are retrieved by similarity to the user's question, so the
   model sees two or three relevant worked examples rather than all of them.
   =========================================================================== */
INSERT INTO ai.approved_sql_examples
    (question, sql_text, category, tables_used, glossary_terms, verified_by, verified_on, notes)
SELECT v.question, v.sql_text, v.category, v.tables_used, v.glossary_terms,
       v.verified_by, v.verified_on, v.notes
FROM (VALUES
(N'Which five customers have the highest ARR?',
 N'SELECT TOP (5) customer_name, current_arr, segment, region
FROM analytics.vw_customer_360
WHERE customer_status = ''active''
ORDER BY current_arr DESC;',
 'revenue', N'analytics.vw_customer_360', N'ARR, Active customer',
 N'Revenue Operations', '2025-06-01',
 N'Uses the curated view so ARR matches the official definition.'),

(N'Calculate MRR using the official business definition.',
 N'SELECT month_start, tenant_name, mrr, paying_customers
FROM analytics.vw_monthly_recurring_revenue
ORDER BY month_start DESC, tenant_name;',
 'revenue', N'analytics.vw_monthly_recurring_revenue', N'MRR',
 N'Finance', '2025-06-01',
 N'The view already excludes trials and normalises annual plans.'),

(N'Why did churn increase in Q2?',
 N'SELECT month_start, churned_customers, customers_at_start, churned_mrr,
       contraction_mrr, expansion_mrr, new_business_mrr
FROM analytics.vw_churn_metrics
WHERE calendar_quarter = CONCAT(YEAR(month_start), ''-Q2'')
ORDER BY month_start;',
 'churn', N'analytics.vw_churn_metrics', N'Revenue churn, Logo churn, Contraction revenue',
 N'Revenue Operations', '2025-06-01',
 N'Returns both logo and revenue churn plus the offsetting movements, so the answer can explain the cause rather than just the number.'),

(N'Show customers with more than three SLA breaches.',
 N'SELECT customer_name, segment, sla_breaches, current_arr
FROM analytics.vw_customer_360
WHERE sla_breaches > 3
ORDER BY sla_breaches DESC, current_arr DESC;',
 'support', N'analytics.vw_customer_360', N'SLA breach',
 N'Support Operations', '2025-06-01',
 N'Strictly greater than three, matching the wording of the question.'),

(N'Compare the contractual response time with the actual response time for a customer.',
 N'SELECT ticket_number, priority, opened_at_utc, sla_version,
       target_first_response_minutes, actual_first_response_minutes,
       first_response_breached
FROM analytics.vw_sla_performance
WHERE customer_name = @customer_name
ORDER BY opened_at_utc DESC;',
 'support', N'analytics.vw_sla_performance', N'SLA breach, First-response time',
 N'Support Operations', '2025-06-01',
 N'The view resolves the SLA version in force when the ticket was opened. Parameterised on customer_name.'),

(N'Which customers are at risk according to usage, unpaid invoices and support tickets?',
 N'SELECT customer_name, segment, current_arr, risk_signal_count,
       signal_unpaid_invoice, signal_repeated_sla_breach,
       signal_high_open_tickets, signal_low_usage, signal_low_health
FROM analytics.vw_customer_risk
WHERE risk_signal_count >= 2
ORDER BY current_arr DESC;',
 'risk', N'analytics.vw_customer_risk', N'At-risk customer',
 N'Customer Success', '2025-06-01',
 N'Returns the individual signals so the answer can state why each customer is at risk.'),

(N'What caused the June service incident and which customers were affected?',
 N'SELECT incident_code, incident_title, severity, started_at_utc, resolved_at_utc,
       total_duration_minutes, root_cause, postmortem_doc_id,
       customer_name, impact_level, downtime_minutes, credit_amount
FROM analytics.vw_incident_impact
WHERE started_at_utc >= ''2025-06-01'' AND started_at_utc < ''2025-07-01''
ORDER BY severity, downtime_minutes DESC;',
 'incident', N'analytics.vw_incident_impact', N'',
 N'Support Operations', '2025-06-01',
 N'postmortem_doc_id lets the answer cite the postmortem document alongside the database facts.'),

(N'How many active customers do we have per region?',
 N'SELECT region, COUNT(*) AS active_customers, SUM(current_mrr) AS total_mrr
FROM analytics.vw_customer_360
WHERE customer_status = ''active'' AND active_subscriptions > 0
GROUP BY region
ORDER BY active_customers DESC;',
 'revenue', N'analytics.vw_customer_360', N'Active customer, MRR',
 N'Revenue Operations', '2025-06-01',
 N'Applies the full active-customer definition, not just status.'),

(N'What is the total overdue amount by customer?',
 N'SELECT customer_name, open_invoices, overdue_amount, current_arr
FROM analytics.vw_customer_360
WHERE overdue_amount > 0
ORDER BY overdue_amount DESC;',
 'billing', N'analytics.vw_customer_360', N'Overdue invoice',
 N'Finance', '2025-06-01',
 N'Outstanding balance, not invoice total.'),

(N'Which products have the lowest seat adoption?',
 N'SELECT p.product_name,
       AVG(CAST(u.active_users AS float)) AS avg_active_users,
       AVG(CAST(s.seats AS float))        AS avg_seats_purchased,
       AVG(CAST(u.active_users AS float)) / NULLIF(AVG(CAST(s.seats AS float)), 0) AS adoption_ratio
FROM core.usage_daily u
JOIN core.products p      ON p.product_id = u.product_id
JOIN core.subscriptions s ON s.customer_id = u.customer_id AND s.status = ''active''
WHERE u.usage_date >= DATEADD(DAY, -30, CAST(GETDATE() AS date))
GROUP BY p.product_name
ORDER BY adoption_ratio;',
 'usage', N'core.usage_daily, core.products, core.subscriptions', N'Product adoption',
 N'Customer Success', '2025-06-01',
 N'NULLIF guards the division. Base tables are used because no curated adoption view exists yet.')
) AS v(question, sql_text, category, tables_used, glossary_terms, verified_by, verified_on, notes)
WHERE NOT EXISTS (
    SELECT 1 FROM ai.approved_sql_examples e WHERE e.question = v.question
);
GO

INSERT INTO ai.schema_version (schema_version, script_name, notes)
SELECT '1.0.0', '007_seed_reference_data.sql', 'Reference data, glossary and approved SQL examples seeded.'
WHERE NOT EXISTS (
    SELECT 1 FROM ai.schema_version WHERE script_name = '007_seed_reference_data.sql'
);
GO

PRINT '007_seed_reference_data.sql complete.';
GO
