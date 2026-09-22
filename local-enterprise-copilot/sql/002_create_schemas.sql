/* ===========================================================================
   002_create_schemas.sql
   Logical separation of concerns. Each schema has a distinct owner and a
   distinct exposure level to the AI layer.

     core      - customers, products, plans, subscriptions, usage
     billing   - invoices, payments, refunds
     support   - tickets, SLA policies and breaches, incidents
     analytics - curated, safe, read-optimised views (the AI's preferred surface)
     security  - application users, access groups, tenant permissions
     ai        - semantic layer: glossary, approved SQL examples, audit trail

   The generated-SQL guard allows analytics/core/billing/support and refuses
   security/ai. That is deliberate: the AI must never read the table that
   records what the AI did, nor the table that defines who may see what.
   =========================================================================== */

USE [EnterpriseCopilot];
GO

IF SCHEMA_ID(N'core')      IS NULL EXEC(N'CREATE SCHEMA [core];');
IF SCHEMA_ID(N'billing')   IS NULL EXEC(N'CREATE SCHEMA [billing];');
IF SCHEMA_ID(N'support')   IS NULL EXEC(N'CREATE SCHEMA [support];');
IF SCHEMA_ID(N'analytics') IS NULL EXEC(N'CREATE SCHEMA [analytics];');
IF SCHEMA_ID(N'security')  IS NULL EXEC(N'CREATE SCHEMA [security];');
IF SCHEMA_ID(N'ai')        IS NULL EXEC(N'CREATE SCHEMA [ai];');
GO

PRINT '002_create_schemas.sql complete.';
GO
