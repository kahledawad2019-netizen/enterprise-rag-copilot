/* ===========================================================================
   003_create_tables.sql
   Normalised schema for the fictional B2B SaaS company "Northwind Cloud".

   Schema version: 1.0.0

   Design notes
   ------------
   * Every business table carries `tenant_id`. Tenant isolation is enforced in
     three places: this column, the analytics views, and the SQL guard's
     injected tenant predicate. A column alone is not isolation.
   * Money is DECIMAL(19,4), never FLOAT. Binary floating point cannot
     represent 0.10 exactly, and revenue totals must reconcile.
   * Dates that represent a business day are DATE. Points in time are
     DATETIME2(3) and are stored in UTC; `*_utc` naming makes that explicit,
     which is what makes the time-zone edge cases in the synthetic data
     meaningful rather than ambiguous.
   * Optional columns are genuinely NULLable so the generator can produce the
     missing-value cases the evaluation set depends on.
   * Idempotent: every CREATE is guarded, so the script can be re-run.
   =========================================================================== */

USE [EnterpriseCopilot];
GO

/* ---------------------------------------------------------------------------
   core.tenants - business units. The root of the isolation model.
   --------------------------------------------------------------------------- */
IF OBJECT_ID(N'core.tenants', N'U') IS NULL
CREATE TABLE core.tenants (
    tenant_id           INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_tenants PRIMARY KEY,
    tenant_code         VARCHAR(20)     NOT NULL CONSTRAINT UQ_tenants_code UNIQUE,
    tenant_name         NVARCHAR(120)   NOT NULL,
    region              VARCHAR(40)     NOT NULL,
    default_currency    CHAR(3)         NOT NULL CONSTRAINT DF_tenants_ccy DEFAULT ('USD'),
    time_zone           VARCHAR(60)     NOT NULL CONSTRAINT DF_tenants_tz  DEFAULT ('UTC'),
    is_active           BIT             NOT NULL CONSTRAINT DF_tenants_active DEFAULT (1),
    created_at_utc      DATETIME2(3)    NOT NULL CONSTRAINT DF_tenants_created DEFAULT (SYSUTCDATETIME())
);
GO

/* ---------------------------------------------------------------------------
   core.customers
   `status` is the lifecycle state; `churn_date` is the authoritative churn
   marker. Both exist because "is this customer churned?" and "when did they
   churn?" are different questions and the glossary defines each separately.
   --------------------------------------------------------------------------- */
IF OBJECT_ID(N'core.customers', N'U') IS NULL
CREATE TABLE core.customers (
    customer_id         INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_customers PRIMARY KEY,
    tenant_id           INT             NOT NULL,
    customer_code       VARCHAR(20)     NOT NULL CONSTRAINT UQ_customers_code UNIQUE,
    legal_name          NVARCHAR(200)   NOT NULL,
    display_name        NVARCHAR(200)   NOT NULL,
    industry            NVARCHAR(80)        NULL,   -- optional: missing-value case
    segment             VARCHAR(20)     NOT NULL,   -- SMB | MidMarket | Enterprise
    country_code        CHAR(2)         NOT NULL,
    region              VARCHAR(40)     NOT NULL,
    employee_count      INT                 NULL,   -- optional
    billing_currency    CHAR(3)         NOT NULL CONSTRAINT DF_customers_ccy DEFAULT ('USD'),
    time_zone           VARCHAR(60)     NOT NULL CONSTRAINT DF_customers_tz DEFAULT ('UTC'),
    signup_date         DATE            NOT NULL,
    churn_date          DATE                NULL,   -- NULL = not churned
    status              VARCHAR(20)     NOT NULL,   -- active|churned|trial|suspended
    is_reactivated      BIT             NOT NULL CONSTRAINT DF_customers_react DEFAULT (0),
    created_at_utc      DATETIME2(3)    NOT NULL CONSTRAINT DF_customers_created DEFAULT (SYSUTCDATETIME()),
    updated_at_utc      DATETIME2(3)    NOT NULL CONSTRAINT DF_customers_updated DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT FK_customers_tenant  FOREIGN KEY (tenant_id) REFERENCES core.tenants(tenant_id),
    CONSTRAINT CK_customers_status  CHECK (status IN ('active','churned','trial','suspended')),
    CONSTRAINT CK_customers_segment CHECK (segment IN ('SMB','MidMarket','Enterprise')),
    -- A churned customer must have a churn date, and vice versa. This is the
    -- kind of invariant that makes churn metrics trustworthy.
    CONSTRAINT CK_customers_churn   CHECK (
        (status = 'churned' AND churn_date IS NOT NULL)
        OR (status <> 'churned' AND churn_date IS NULL)
    )
);
GO

/* Synthetic contacts only. No real personal data is ever stored here. */
IF OBJECT_ID(N'core.customer_contacts', N'U') IS NULL
CREATE TABLE core.customer_contacts (
    contact_id          INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_contacts PRIMARY KEY,
    customer_id         INT             NOT NULL,
    tenant_id           INT             NOT NULL,
    full_name           NVARCHAR(150)   NOT NULL,
    email               NVARCHAR(200)   NOT NULL,
    phone               VARCHAR(40)         NULL,
    role_title          NVARCHAR(100)       NULL,
    is_primary          BIT             NOT NULL CONSTRAINT DF_contacts_primary DEFAULT (0),
    is_synthetic        BIT             NOT NULL CONSTRAINT DF_contacts_synth DEFAULT (1),
    created_at_utc      DATETIME2(3)    NOT NULL CONSTRAINT DF_contacts_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT FK_contacts_customer FOREIGN KEY (customer_id) REFERENCES core.customers(customer_id),
    CONSTRAINT FK_contacts_tenant   FOREIGN KEY (tenant_id)   REFERENCES core.tenants(tenant_id)
);
GO

IF OBJECT_ID(N'core.products', N'U') IS NULL
CREATE TABLE core.products (
    product_id          INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_products PRIMARY KEY,
    product_code        VARCHAR(20)     NOT NULL CONSTRAINT UQ_products_code UNIQUE,
    product_name        NVARCHAR(120)   NOT NULL,
    product_family      VARCHAR(40)     NOT NULL,
    description         NVARCHAR(500)       NULL,
    launched_on         DATE            NOT NULL,
    retired_on          DATE                NULL,
    is_active           BIT             NOT NULL CONSTRAINT DF_products_active DEFAULT (1)
);
GO

/* Plans are versioned by effective_from / effective_to so that historical
   pricing can be reconstructed. Invoices must price against the plan version
   that was in force on the invoice date, not today's list price. */
IF OBJECT_ID(N'core.plans', N'U') IS NULL
CREATE TABLE core.plans (
    plan_id             INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_plans PRIMARY KEY,
    product_id          INT             NOT NULL,
    plan_code           VARCHAR(30)     NOT NULL CONSTRAINT UQ_plans_code UNIQUE,
    plan_name           NVARCHAR(120)   NOT NULL,
    tier                VARCHAR(20)     NOT NULL,   -- Starter|Professional|Enterprise
    billing_interval    VARCHAR(10)     NOT NULL,   -- monthly|annual
    list_price_monthly  DECIMAL(19,4)   NOT NULL,
    currency_code       CHAR(3)         NOT NULL CONSTRAINT DF_plans_ccy DEFAULT ('USD'),
    seats_included      INT             NOT NULL CONSTRAINT DF_plans_seats DEFAULT (1),
    effective_from      DATE            NOT NULL,
    effective_to        DATE                NULL,
    is_active           BIT             NOT NULL CONSTRAINT DF_plans_active DEFAULT (1),
    CONSTRAINT FK_plans_product  FOREIGN KEY (product_id) REFERENCES core.products(product_id),
    CONSTRAINT CK_plans_tier     CHECK (tier IN ('Starter','Professional','Enterprise')),
    CONSTRAINT CK_plans_interval CHECK (billing_interval IN ('monthly','annual')),
    CONSTRAINT CK_plans_price    CHECK (list_price_monthly >= 0)
);
GO

/* ---------------------------------------------------------------------------
   core.subscriptions
   mrr_amount is stored, not derived. Storing it captures the negotiated price
   (after discount) which cannot be recovered from the plan list price alone.
   The MRR glossary entry depends on this distinction.
   --------------------------------------------------------------------------- */
IF OBJECT_ID(N'core.subscriptions', N'U') IS NULL
CREATE TABLE core.subscriptions (
    subscription_id     INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_subscriptions PRIMARY KEY,
    customer_id         INT             NOT NULL,
    plan_id             INT             NOT NULL,
    tenant_id           INT             NOT NULL,
    started_on          DATE            NOT NULL,
    ended_on            DATE                NULL,   -- NULL = still running
    status              VARCHAR(20)     NOT NULL,   -- active|cancelled|expired|trial|paused
    seats               INT             NOT NULL CONSTRAINT DF_subs_seats DEFAULT (1),
    mrr_amount          DECIMAL(19,4)   NOT NULL,   -- normalised monthly value
    discount_pct        DECIMAL(5,2)    NOT NULL CONSTRAINT DF_subs_discount DEFAULT (0),
    currency_code       CHAR(3)         NOT NULL CONSTRAINT DF_subs_ccy DEFAULT ('USD'),
    is_trial            BIT             NOT NULL CONSTRAINT DF_subs_trial DEFAULT (0),
    auto_renew          BIT             NOT NULL CONSTRAINT DF_subs_renew DEFAULT (1),
    cancellation_reason NVARCHAR(200)       NULL,
    created_at_utc      DATETIME2(3)    NOT NULL CONSTRAINT DF_subs_created DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT FK_subs_customer FOREIGN KEY (customer_id) REFERENCES core.customers(customer_id),
    CONSTRAINT FK_subs_plan     FOREIGN KEY (plan_id)     REFERENCES core.plans(plan_id),
    CONSTRAINT FK_subs_tenant   FOREIGN KEY (tenant_id)   REFERENCES core.tenants(tenant_id),
    CONSTRAINT CK_subs_status   CHECK (status IN ('active','cancelled','expired','trial','paused')),
    CONSTRAINT CK_subs_dates    CHECK (ended_on IS NULL OR ended_on >= started_on),
    CONSTRAINT CK_subs_mrr      CHECK (mrr_amount >= 0)
);
GO

/* Every upgrade, downgrade, seat change, cancellation and reactivation.
   This table is what makes expansion vs contraction revenue computable. */
IF OBJECT_ID(N'core.subscription_changes', N'U') IS NULL
CREATE TABLE core.subscription_changes (
    change_id           INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_sub_changes PRIMARY KEY,
    subscription_id     INT             NOT NULL,
    customer_id         INT             NOT NULL,
    tenant_id           INT             NOT NULL,
    change_type         VARCHAR(20)     NOT NULL,  -- new|upgrade|downgrade|seat_change|cancel|reactivate
    changed_on          DATE            NOT NULL,
    from_plan_id        INT                 NULL,
    to_plan_id          INT                 NULL,
    from_seats          INT                 NULL,
    to_seats            INT                 NULL,
    from_mrr            DECIMAL(19,4)       NULL,
    to_mrr              DECIMAL(19,4)       NULL,
    mrr_delta           DECIMAL(19,4)   NOT NULL CONSTRAINT DF_chg_delta DEFAULT (0),
    reason              NVARCHAR(300)       NULL,
    CONSTRAINT FK_chg_sub      FOREIGN KEY (subscription_id) REFERENCES core.subscriptions(subscription_id),
    CONSTRAINT FK_chg_customer FOREIGN KEY (customer_id)     REFERENCES core.customers(customer_id),
    CONSTRAINT FK_chg_tenant   FOREIGN KEY (tenant_id)       REFERENCES core.tenants(tenant_id),
    CONSTRAINT CK_chg_type     CHECK (change_type IN
        ('new','upgrade','downgrade','seat_change','cancel','reactivate'))
);
GO

/* Daily aggregate rather than raw events: three years of per-event usage would
   dominate the database size for no analytical gain at this scale. */
IF OBJECT_ID(N'core.usage_daily', N'U') IS NULL
CREATE TABLE core.usage_daily (
    usage_id            BIGINT          NOT NULL IDENTITY(1,1) CONSTRAINT PK_usage_daily PRIMARY KEY,
    customer_id         INT             NOT NULL,
    product_id          INT             NOT NULL,
    tenant_id           INT             NOT NULL,
    usage_date          DATE            NOT NULL,
    active_users        INT             NOT NULL CONSTRAINT DF_usage_users DEFAULT (0),
    sessions            INT             NOT NULL CONSTRAINT DF_usage_sessions DEFAULT (0),
    api_calls           BIGINT          NOT NULL CONSTRAINT DF_usage_api DEFAULT (0),
    storage_gb          DECIMAL(12,3)   NOT NULL CONSTRAINT DF_usage_storage DEFAULT (0),
    CONSTRAINT FK_usage_customer FOREIGN KEY (customer_id) REFERENCES core.customers(customer_id),
    CONSTRAINT FK_usage_product  FOREIGN KEY (product_id)  REFERENCES core.products(product_id),
    CONSTRAINT FK_usage_tenant   FOREIGN KEY (tenant_id)   REFERENCES core.tenants(tenant_id),
    CONSTRAINT UQ_usage_grain    UNIQUE (customer_id, product_id, usage_date)
);
GO

/* ===========================================================================
   billing
   =========================================================================== */
IF OBJECT_ID(N'billing.invoices', N'U') IS NULL
CREATE TABLE billing.invoices (
    invoice_id          INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_invoices PRIMARY KEY,
    invoice_number      VARCHAR(30)     NOT NULL CONSTRAINT UQ_invoices_number UNIQUE,
    customer_id         INT             NOT NULL,
    subscription_id     INT                 NULL,
    tenant_id           INT             NOT NULL,
    issue_date          DATE            NOT NULL,
    due_date            DATE            NOT NULL,
    period_start        DATE            NOT NULL,
    period_end          DATE            NOT NULL,
    currency_code       CHAR(3)         NOT NULL CONSTRAINT DF_inv_ccy DEFAULT ('USD'),
    subtotal_amount     DECIMAL(19,4)   NOT NULL,
    tax_amount          DECIMAL(19,4)   NOT NULL CONSTRAINT DF_inv_tax DEFAULT (0),
    total_amount        DECIMAL(19,4)   NOT NULL,
    amount_paid         DECIMAL(19,4)   NOT NULL CONSTRAINT DF_inv_paid DEFAULT (0),
    status              VARCHAR(20)     NOT NULL,  -- draft|open|paid|overdue|void|partial
    paid_date           DATE                NULL,
    CONSTRAINT FK_inv_customer FOREIGN KEY (customer_id)     REFERENCES core.customers(customer_id),
    CONSTRAINT FK_inv_sub      FOREIGN KEY (subscription_id) REFERENCES core.subscriptions(subscription_id),
    CONSTRAINT FK_inv_tenant   FOREIGN KEY (tenant_id)       REFERENCES core.tenants(tenant_id),
    CONSTRAINT CK_inv_status   CHECK (status IN ('draft','open','paid','overdue','void','partial')),
    CONSTRAINT CK_inv_dates    CHECK (due_date >= issue_date AND period_end >= period_start),
    CONSTRAINT CK_inv_total    CHECK (total_amount >= 0)
);
GO

IF OBJECT_ID(N'billing.invoice_items', N'U') IS NULL
CREATE TABLE billing.invoice_items (
    invoice_item_id     INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_invoice_items PRIMARY KEY,
    invoice_id          INT             NOT NULL,
    product_id          INT                 NULL,
    plan_id             INT                 NULL,
    description         NVARCHAR(300)   NOT NULL,
    quantity            DECIMAL(12,3)   NOT NULL CONSTRAINT DF_item_qty DEFAULT (1),
    unit_price          DECIMAL(19,4)   NOT NULL,
    line_amount         DECIMAL(19,4)   NOT NULL,
    CONSTRAINT FK_item_invoice FOREIGN KEY (invoice_id) REFERENCES billing.invoices(invoice_id),
    CONSTRAINT FK_item_product FOREIGN KEY (product_id) REFERENCES core.products(product_id),
    CONSTRAINT FK_item_plan    FOREIGN KEY (plan_id)    REFERENCES core.plans(plan_id)
);
GO

IF OBJECT_ID(N'billing.payments', N'U') IS NULL
CREATE TABLE billing.payments (
    payment_id          INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_payments PRIMARY KEY,
    invoice_id          INT             NOT NULL,
    customer_id         INT             NOT NULL,
    tenant_id           INT             NOT NULL,
    payment_date        DATE            NOT NULL,
    amount              DECIMAL(19,4)   NOT NULL,
    currency_code       CHAR(3)         NOT NULL CONSTRAINT DF_pay_ccy DEFAULT ('USD'),
    method              VARCHAR(20)     NOT NULL,  -- card|ach|wire|check
    status              VARCHAR(20)     NOT NULL,  -- succeeded|failed|pending
    reference           VARCHAR(60)         NULL,
    days_late           INT             NOT NULL CONSTRAINT DF_pay_late DEFAULT (0),
    CONSTRAINT FK_pay_invoice  FOREIGN KEY (invoice_id)  REFERENCES billing.invoices(invoice_id),
    CONSTRAINT FK_pay_customer FOREIGN KEY (customer_id) REFERENCES core.customers(customer_id),
    CONSTRAINT FK_pay_tenant   FOREIGN KEY (tenant_id)   REFERENCES core.tenants(tenant_id),
    CONSTRAINT CK_pay_status   CHECK (status IN ('succeeded','failed','pending')),
    CONSTRAINT CK_pay_method   CHECK (method IN ('card','ach','wire','check'))
);
GO

IF OBJECT_ID(N'billing.refunds', N'U') IS NULL
CREATE TABLE billing.refunds (
    refund_id           INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_refunds PRIMARY KEY,
    invoice_id          INT             NOT NULL,
    payment_id          INT                 NULL,
    customer_id         INT             NOT NULL,
    tenant_id           INT             NOT NULL,
    refund_date         DATE            NOT NULL,
    amount              DECIMAL(19,4)   NOT NULL,
    currency_code       CHAR(3)         NOT NULL CONSTRAINT DF_ref_ccy DEFAULT ('USD'),
    reason_code         VARCHAR(30)     NOT NULL,  -- service_credit|billing_error|downgrade|cancellation|sla_credit
    reason              NVARCHAR(300)       NULL,
    is_partial          BIT             NOT NULL CONSTRAINT DF_ref_partial DEFAULT (0),
    approved_by         NVARCHAR(100)       NULL,
    CONSTRAINT FK_ref_invoice  FOREIGN KEY (invoice_id)  REFERENCES billing.invoices(invoice_id),
    CONSTRAINT FK_ref_payment  FOREIGN KEY (payment_id)  REFERENCES billing.payments(payment_id),
    CONSTRAINT FK_ref_customer FOREIGN KEY (customer_id) REFERENCES core.customers(customer_id),
    CONSTRAINT FK_ref_tenant   FOREIGN KEY (tenant_id)   REFERENCES core.tenants(tenant_id),
    CONSTRAINT CK_ref_amount   CHECK (amount > 0)
);
GO

/* ===========================================================================
   support
   =========================================================================== */

/* SLA policies are versioned. A breach must be judged against the policy that
   was in force when the ticket was opened, which is exactly the kind of
   nuance a schema-only Text-to-SQL prompt gets wrong. */
IF OBJECT_ID(N'support.sla_policies', N'U') IS NULL
CREATE TABLE support.sla_policies (
    sla_policy_id           INT         NOT NULL IDENTITY(1,1) CONSTRAINT PK_sla_policies PRIMARY KEY,
    policy_code             VARCHAR(40) NOT NULL,
    plan_tier               VARCHAR(20) NOT NULL,
    priority                VARCHAR(10) NOT NULL,  -- P1|P2|P3|P4
    first_response_minutes  INT         NOT NULL,
    resolution_minutes      INT         NOT NULL,
    coverage_hours          VARCHAR(20) NOT NULL,  -- 24x7|business_hours
    uptime_target_pct       DECIMAL(5,2)    NULL,
    version                 VARCHAR(10) NOT NULL,
    effective_from          DATE        NOT NULL,
    effective_to            DATE            NULL,
    is_current              BIT         NOT NULL CONSTRAINT DF_sla_current DEFAULT (1),
    CONSTRAINT UQ_sla_policy  UNIQUE (policy_code, version),
    CONSTRAINT CK_sla_tier     CHECK (plan_tier IN ('Starter','Professional','Enterprise')),
    CONSTRAINT CK_sla_priority CHECK (priority IN ('P1','P2','P3','P4'))
);
GO

IF OBJECT_ID(N'support.tickets', N'U') IS NULL
CREATE TABLE support.tickets (
    ticket_id           INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_tickets PRIMARY KEY,
    ticket_number       VARCHAR(30)     NOT NULL CONSTRAINT UQ_tickets_number UNIQUE,
    customer_id         INT             NOT NULL,
    product_id          INT                 NULL,
    tenant_id           INT             NOT NULL,
    opened_at_utc       DATETIME2(3)    NOT NULL,
    first_response_at_utc DATETIME2(3)      NULL,  -- NULL = never responded
    resolved_at_utc     DATETIME2(3)        NULL,
    closed_at_utc       DATETIME2(3)        NULL,
    priority            VARCHAR(10)     NOT NULL,
    category            VARCHAR(40)     NOT NULL,
    channel             VARCHAR(20)     NOT NULL,  -- email|portal|phone|chat
    status              VARCHAR(20)     NOT NULL,  -- open|pending|resolved|closed|escalated
    subject             NVARCHAR(300)   NOT NULL,
    satisfaction_score  TINYINT             NULL,  -- 1..5, often missing
    CONSTRAINT FK_tkt_customer FOREIGN KEY (customer_id) REFERENCES core.customers(customer_id),
    CONSTRAINT FK_tkt_product  FOREIGN KEY (product_id)  REFERENCES core.products(product_id),
    CONSTRAINT FK_tkt_tenant   FOREIGN KEY (tenant_id)   REFERENCES core.tenants(tenant_id),
    CONSTRAINT CK_tkt_priority CHECK (priority IN ('P1','P2','P3','P4')),
    CONSTRAINT CK_tkt_status   CHECK (status IN ('open','pending','resolved','closed','escalated')),
    CONSTRAINT CK_tkt_csat     CHECK (satisfaction_score IS NULL OR satisfaction_score BETWEEN 1 AND 5)
);
GO

IF OBJECT_ID(N'support.ticket_events', N'U') IS NULL
CREATE TABLE support.ticket_events (
    ticket_event_id     BIGINT          NOT NULL IDENTITY(1,1) CONSTRAINT PK_ticket_events PRIMARY KEY,
    ticket_id           INT             NOT NULL,
    tenant_id           INT             NOT NULL,
    event_at_utc        DATETIME2(3)    NOT NULL,
    event_type          VARCHAR(30)     NOT NULL,  -- created|first_response|escalated|status_change|resolved|closed|comment
    actor_type          VARCHAR(20)     NOT NULL,  -- customer|agent|system
    notes               NVARCHAR(500)       NULL,
    CONSTRAINT FK_tevt_ticket FOREIGN KEY (ticket_id) REFERENCES support.tickets(ticket_id),
    CONSTRAINT FK_tevt_tenant FOREIGN KEY (tenant_id) REFERENCES core.tenants(tenant_id)
);
GO

IF OBJECT_ID(N'support.sla_breaches', N'U') IS NULL
CREATE TABLE support.sla_breaches (
    breach_id           INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_sla_breaches PRIMARY KEY,
    ticket_id           INT             NOT NULL,
    customer_id         INT             NOT NULL,
    sla_policy_id       INT             NOT NULL,
    tenant_id           INT             NOT NULL,
    breach_type         VARCHAR(20)     NOT NULL,  -- first_response|resolution
    target_minutes      INT             NOT NULL,
    actual_minutes      INT             NOT NULL,
    breach_minutes      INT             NOT NULL,  -- actual - target, always > 0
    detected_at_utc     DATETIME2(3)    NOT NULL,
    credit_issued       BIT             NOT NULL CONSTRAINT DF_breach_credit DEFAULT (0),
    CONSTRAINT FK_brc_ticket   FOREIGN KEY (ticket_id)     REFERENCES support.tickets(ticket_id),
    CONSTRAINT FK_brc_customer FOREIGN KEY (customer_id)   REFERENCES core.customers(customer_id),
    CONSTRAINT FK_brc_policy   FOREIGN KEY (sla_policy_id) REFERENCES support.sla_policies(sla_policy_id),
    CONSTRAINT FK_brc_tenant   FOREIGN KEY (tenant_id)     REFERENCES core.tenants(tenant_id),
    CONSTRAINT CK_brc_type     CHECK (breach_type IN ('first_response','resolution')),
    CONSTRAINT CK_brc_positive CHECK (breach_minutes > 0)
);
GO

IF OBJECT_ID(N'support.incidents', N'U') IS NULL
CREATE TABLE support.incidents (
    incident_id         INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_incidents PRIMARY KEY,
    incident_code       VARCHAR(30)     NOT NULL CONSTRAINT UQ_incidents_code UNIQUE,
    title               NVARCHAR(300)   NOT NULL,
    severity            VARCHAR(10)     NOT NULL,  -- SEV1|SEV2|SEV3
    product_id          INT                 NULL,  -- NULL = platform-wide
    affected_region     VARCHAR(40)         NULL,  -- NULL = all regions
    started_at_utc      DATETIME2(3)    NOT NULL,
    detected_at_utc     DATETIME2(3)    NOT NULL,
    resolved_at_utc     DATETIME2(3)        NULL,
    status              VARCHAR(20)     NOT NULL,  -- investigating|identified|monitoring|resolved
    root_cause          NVARCHAR(1000)      NULL,
    postmortem_doc_id   VARCHAR(40)         NULL,  -- links to the document corpus
    CONSTRAINT FK_inc_product  FOREIGN KEY (product_id) REFERENCES core.products(product_id),
    CONSTRAINT CK_inc_severity CHECK (severity IN ('SEV1','SEV2','SEV3')),
    CONSTRAINT CK_inc_status   CHECK (status IN ('investigating','identified','monitoring','resolved'))
);
GO

IF OBJECT_ID(N'support.incident_impact', N'U') IS NULL
CREATE TABLE support.incident_impact (
    impact_id           INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_incident_impact PRIMARY KEY,
    incident_id         INT             NOT NULL,
    customer_id         INT             NOT NULL,
    tenant_id           INT             NOT NULL,
    impact_level        VARCHAR(20)     NOT NULL,  -- full_outage|degraded|minimal
    downtime_minutes    INT             NOT NULL CONSTRAINT DF_imp_downtime DEFAULT (0),
    credit_amount       DECIMAL(19,4)   NOT NULL CONSTRAINT DF_imp_credit DEFAULT (0),
    CONSTRAINT FK_imp_incident FOREIGN KEY (incident_id) REFERENCES support.incidents(incident_id),
    CONSTRAINT FK_imp_customer FOREIGN KEY (customer_id) REFERENCES core.customers(customer_id),
    CONSTRAINT FK_imp_tenant   FOREIGN KEY (tenant_id)   REFERENCES core.tenants(tenant_id),
    CONSTRAINT UQ_imp_grain    UNIQUE (incident_id, customer_id),
    CONSTRAINT CK_imp_level    CHECK (impact_level IN ('full_outage','degraded','minimal'))
);
GO

/* ===========================================================================
   analytics - materialised snapshots (the views live in 005)
   =========================================================================== */
IF OBJECT_ID(N'analytics.customer_health', N'U') IS NULL
CREATE TABLE analytics.customer_health (
    health_id           INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_customer_health PRIMARY KEY,
    customer_id         INT             NOT NULL,
    tenant_id           INT             NOT NULL,
    snapshot_date       DATE            NOT NULL,
    health_score        DECIMAL(5,2)    NOT NULL,  -- 0..100
    usage_score         DECIMAL(5,2)    NOT NULL,
    support_score       DECIMAL(5,2)    NOT NULL,
    billing_score       DECIMAL(5,2)    NOT NULL,
    risk_band           VARCHAR(10)     NOT NULL,  -- low|medium|high|critical
    churn_risk_pct      DECIMAL(5,2)    NOT NULL,
    CONSTRAINT FK_health_customer FOREIGN KEY (customer_id) REFERENCES core.customers(customer_id),
    CONSTRAINT FK_health_tenant   FOREIGN KEY (tenant_id)   REFERENCES core.tenants(tenant_id),
    CONSTRAINT UQ_health_grain    UNIQUE (customer_id, snapshot_date),
    CONSTRAINT CK_health_band     CHECK (risk_band IN ('low','medium','high','critical'))
);
GO

/* ===========================================================================
   ai - the semantic layer and the AI's own audit trail
   =========================================================================== */

/* The business glossary is the reason this system can answer "calculate MRR
   using the official definition". Schema alone cannot express that annual
   plans are divided by 12, or that trials are excluded. */
IF OBJECT_ID(N'ai.business_glossary', N'U') IS NULL
CREATE TABLE ai.business_glossary (
    term_id             INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_glossary PRIMARY KEY,
    term                NVARCHAR(100)   NOT NULL,
    definition          NVARCHAR(MAX)   NOT NULL,
    sql_guidance        NVARCHAR(MAX)   NOT NULL,
    owner               NVARCHAR(100)   NOT NULL,
    version             VARCHAR(10)     NOT NULL,
    effective_date      DATE            NOT NULL,
    related_tables      NVARCHAR(500)       NULL,
    related_columns     NVARCHAR(1000)      NULL,
    example_calculation NVARCHAR(MAX)       NULL,
    known_exclusions    NVARCHAR(MAX)       NULL,
    is_current          BIT             NOT NULL CONSTRAINT DF_gloss_current DEFAULT (1),
    CONSTRAINT UQ_glossary_term_version UNIQUE (term, version)
);
GO

/* Few-shot examples for Text-to-SQL. These are the single highest-leverage
   input to SQL generation quality. */
IF OBJECT_ID(N'ai.approved_sql_examples', N'U') IS NULL
CREATE TABLE ai.approved_sql_examples (
    example_id          INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_sql_examples PRIMARY KEY,
    question            NVARCHAR(500)   NOT NULL,
    sql_text            NVARCHAR(MAX)   NOT NULL,
    category            VARCHAR(40)     NOT NULL,
    tables_used         NVARCHAR(500)       NULL,
    glossary_terms      NVARCHAR(300)       NULL,
    verified_by         NVARCHAR(100)       NULL,
    verified_on         DATE                NULL,
    is_active           BIT             NOT NULL CONSTRAINT DF_ex_active DEFAULT (1),
    notes               NVARCHAR(500)       NULL
);
GO

/* Every AI action against the database is recorded here, allowed or blocked.
   The generated-SQL guard refuses to read this schema, so the AI cannot read
   or reason about its own audit trail. */
IF OBJECT_ID(N'ai.audit_events', N'U') IS NULL
CREATE TABLE ai.audit_events (
    audit_id            BIGINT          NOT NULL IDENTITY(1,1) CONSTRAINT PK_audit_events PRIMARY KEY,
    trace_id            VARCHAR(40)     NOT NULL,
    occurred_at_utc     DATETIME2(3)    NOT NULL CONSTRAINT DF_audit_at DEFAULT (SYSUTCDATETIME()),
    app_user            NVARCHAR(100)       NULL,
    tenant_id           INT                 NULL,
    route               VARCHAR(30)         NULL,  -- document_rag|text_to_sql|multi_source|clarify|refuse
    question            NVARCHAR(MAX)       NULL,
    generated_sql       NVARCHAR(MAX)       NULL,
    sql_allowed         BIT                 NULL,
    block_reason        NVARCHAR(500)       NULL,
    row_count           INT                 NULL,
    duration_ms         INT                 NULL,
    model_name          VARCHAR(100)        NULL,
    error_category      VARCHAR(60)         NULL
);
GO

/* ===========================================================================
   security - who may see what
   =========================================================================== */
IF OBJECT_ID(N'security.access_groups', N'U') IS NULL
CREATE TABLE security.access_groups (
    group_id            INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_access_groups PRIMARY KEY,
    group_code          VARCHAR(40)     NOT NULL CONSTRAINT UQ_group_code UNIQUE,
    group_name          NVARCHAR(120)   NOT NULL,
    description         NVARCHAR(400)       NULL
);
GO

IF OBJECT_ID(N'security.app_users', N'U') IS NULL
CREATE TABLE security.app_users (
    app_user_id         INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_app_users PRIMARY KEY,
    user_name           NVARCHAR(100)   NOT NULL CONSTRAINT UQ_app_user_name UNIQUE,
    display_name        NVARCHAR(150)   NOT NULL,
    tenant_id           INT             NOT NULL,
    access_groups       NVARCHAR(300)   NOT NULL,  -- comma-separated group codes
    is_admin            BIT             NOT NULL CONSTRAINT DF_user_admin DEFAULT (0),
    is_active           BIT             NOT NULL CONSTRAINT DF_user_active DEFAULT (1),
    CONSTRAINT FK_user_tenant FOREIGN KEY (tenant_id) REFERENCES core.tenants(tenant_id)
);
GO

/* Schema version marker, read by scripts/setup_database.py and the UI. */
IF OBJECT_ID(N'ai.schema_version', N'U') IS NULL
CREATE TABLE ai.schema_version (
    version_id          INT             NOT NULL IDENTITY(1,1) CONSTRAINT PK_schema_version PRIMARY KEY,
    schema_version      VARCHAR(20)     NOT NULL,
    applied_at_utc      DATETIME2(3)    NOT NULL CONSTRAINT DF_sv_at DEFAULT (SYSUTCDATETIME()),
    script_name         VARCHAR(100)    NOT NULL,
    notes               NVARCHAR(400)       NULL
);
GO

IF NOT EXISTS (SELECT 1 FROM ai.schema_version WHERE schema_version = '1.0.0')
    INSERT INTO ai.schema_version (schema_version, script_name, notes)
    VALUES ('1.0.0', '003_create_tables.sql', 'Initial schema: 22 tables across 6 schemas.');
GO

PRINT '003_create_tables.sql complete.';
GO
