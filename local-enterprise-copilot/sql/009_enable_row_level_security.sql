/* ===========================================================================
   009_enable_row_level_security.sql

   Database-enforced tenant isolation. The application sets `tenant_id` with
   sys.sp_set_session_context immediately before every generated query. Even if
   an application validator regresses, SQL Server filters tenant rows itself.

   `dbo` is allowed so setup/validation scripts can operate across the
   synthetic dataset. The deployed copilot_app user must NOT be dbo/db_owner.
   =========================================================================== */
USE [EnterpriseCopilot];
GO

CREATE OR ALTER FUNCTION [security].[fn_tenant_access](@tenant_id INT)
RETURNS TABLE
WITH SCHEMABINDING
AS
RETURN
(
    SELECT 1 AS [allowed]
    WHERE USER_NAME() = N'dbo'
       OR @tenant_id = CONVERT(INT, SESSION_CONTEXT(N'tenant_id'))
);
GO

IF EXISTS (
    SELECT 1 FROM sys.security_policies
    WHERE [name] = N'tenant_filter_policy'
      AND schema_id = SCHEMA_ID(N'security')
)
    DROP SECURITY POLICY [security].[tenant_filter_policy];
GO

CREATE SECURITY POLICY [security].[tenant_filter_policy]
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [core].[tenants],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [core].[customers],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [core].[customer_contacts],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [core].[subscriptions],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [core].[subscription_changes],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [core].[usage_daily],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [billing].[invoices],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [billing].[payments],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [billing].[refunds],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [support].[tickets],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [support].[ticket_events],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [support].[sla_breaches],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [support].[incident_impact],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [analytics].[customer_health],
ADD FILTER PREDICATE [security].[fn_tenant_access]([tenant_id]) ON [security].[app_users]
WITH (STATE = ON);
GO

PRINT 'Row-Level Security tenant_filter_policy is ON.';
GO
