/* ===========================================================================
   002_create_schemas.sql  (PostgreSQL)

   Logical separation of concerns, identical to the SQL Server script:

     core      - customers, products, plans, subscriptions, usage
     billing   - invoices, payments, refunds
     support   - tickets, SLA policies and breaches, incidents
     analytics - curated, safe, read-optimised views (the AI's preferred surface)
     security  - application users, access groups, tenant permissions
     ai        - semantic layer: glossary, approved SQL examples, audit trail

   The generated-SQL guard allows analytics/core/billing/support and refuses
   security/ai. That is deliberate: the AI must never read the table that
   records what the AI did, nor the table that defines who may see what.

   ---------------------------------------------------------------------------
   Translation notes
   ---------------------------------------------------------------------------
   T-SQL needed `IF SCHEMA_ID(...) IS NULL EXEC(N'CREATE SCHEMA ...')` because
   CREATE SCHEMA must be the first statement in its batch and has no IF NOT
   EXISTS. PostgreSQL has CREATE SCHEMA IF NOT EXISTS, so the dynamic-SQL
   wrapper disappears entirely.

   `security` is a legal schema name in PostgreSQL. It is not reserved.
   =========================================================================== */

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS billing;
CREATE SCHEMA IF NOT EXISTS support;
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS security;
CREATE SCHEMA IF NOT EXISTS ai;

/* ---------------------------------------------------------------------------
   Lock down future objects before any exist.

   PostgreSQL grants CREATE and USAGE on the `public` schema to PUBLIC by
   default in versions before 15, and USAGE thereafter. Nothing in this
   application uses `public`, and leaving it writable is how an unrelated
   object ends up somewhere the guard does not check.

   This is also the first place the DENY gap shows up. SQL Server can say
   "DENY SELECT ON SCHEMA::security" and that survives a later mistaken GRANT.
   PostgreSQL has no DENY: privileges are grant-only, so the equivalent is to
   revoke from PUBLIC and never grant. See 006 for the full consequence.
   --------------------------------------------------------------------------- */
REVOKE ALL ON SCHEMA public FROM PUBLIC;

DO $$
BEGIN
    RAISE NOTICE '002_create_schemas.sql complete: 6 schemas present.';
END
$$;
