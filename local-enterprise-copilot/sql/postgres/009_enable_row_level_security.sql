/* ===========================================================================
   009_enable_row_level_security.sql  (PostgreSQL)

   HAND-TRANSLATED from sql/009_enable_row_level_security.sql.

   The backend sets `app.tenant_id` on every transaction before generated SQL
   runs. Missing tenant context produces NULL and therefore zero visible rows.
   ENABLE + FORCE makes policies apply even to the table owner; superusers and
   roles carrying BYPASSRLS still bypass PostgreSQL RLS and must never be used
   by the application.
   =========================================================================== */

DO $rls$
DECLARE
    target_table text;
    tenant_expression constant text :=
        'tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::int';
BEGIN
    FOREACH target_table IN ARRAY ARRAY[
        'core.tenants',
        'core.customers',
        'core.customer_contacts',
        'core.subscriptions',
        'core.subscription_changes',
        'core.usage_daily',
        'billing.invoices',
        'billing.payments',
        'billing.refunds',
        'support.tickets',
        'support.ticket_events',
        'support.sla_breaches',
        'support.incident_impact',
        'analytics.customer_health',
        'security.app_users'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', target_table);
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', target_table);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %s', target_table);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %s USING (%s) WITH CHECK (%s)',
            target_table,
            tenant_expression,
            tenant_expression
        );
    END LOOP;
END
$rls$;

DO $$ BEGIN RAISE NOTICE 'PostgreSQL tenant RLS is enabled and forced.'; END $$;

