/* ===========================================================================
   006_create_security.sql
   Schema version: 1.0.0

   The application executes SQL written by a language model. That SQL is
   untrusted input, so there are two independent layers of defence:

     Layer 1  src/enterprise_copilot/security/sql_guard.py
              Parses the generated SQL with sqlglot and refuses anything that
              is not a single read, touches a forbidden schema or column, or
              omits the tenant predicate.

     Layer 2  THIS SCRIPT
              A database principal that physically cannot write, so a defeated
              Layer 1 still cannot damage or exfiltrate data.

   Layer 1 is application code: a bug or a refactor can weaken it. Layer 2 is
   enforced by the database engine. Ship both.

   ---------------------------------------------------------------------------
   IMPORTANT - read before running
   ---------------------------------------------------------------------------
   Path A below needs the instance in MIXED MODE. The reference instance was
   inspected and reported:

       SELECT CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS int)  -->  1

   which means Windows Authentication ONLY, and CREATE LOGIN ... WITH PASSWORD
   will fail. Enabling Mixed Mode changes a server-level security setting and
   requires a service restart, so this script does NOT do it for you.

   To enable it (one time, by the machine owner):
     1. SSMS -> right-click the instance -> Properties -> Security
     2. Select "SQL Server and Windows Authentication mode"
     3. Restart the service:  Restart-Service 'MSSQL$SQLEXPRESS'
     4. Re-run this script.

   If you prefer not to change the server, use Path B instead.
   See docs/architecture_decisions/ADR-003-sql-server-readonly-principal.md
   =========================================================================== */

USE [EnterpriseCopilot];
GO

/* ---------------------------------------------------------------------------
   Step 1 - a read-only database ROLE.
   Roles work identically for SQL logins and Windows logins, so both Path A and
   Path B end up granting exactly the same rights. Define the permissions once.
   --------------------------------------------------------------------------- */
IF DATABASE_PRINCIPAL_ID(N'copilot_readonly') IS NULL
BEGIN
    CREATE ROLE [copilot_readonly];
    PRINT 'Created role copilot_readonly.';
END
GO

/* Read access to the business schemas only. */
GRANT SELECT ON SCHEMA::[analytics] TO [copilot_readonly];
GRANT SELECT ON SCHEMA::[core]      TO [copilot_readonly];
GRANT SELECT ON SCHEMA::[billing]   TO [copilot_readonly];
GRANT SELECT ON SCHEMA::[support]   TO [copilot_readonly];
GO

/* The glossary and the approved examples are prompt inputs, so they must be
   readable. Everything else in `ai` is the AI's own audit trail. */
GRANT SELECT ON OBJECT::[ai].[business_glossary]      TO [copilot_readonly];
GRANT SELECT ON OBJECT::[ai].[approved_sql_examples]  TO [copilot_readonly];
GRANT SELECT ON OBJECT::[ai].[schema_version]         TO [copilot_readonly];
GO

/* The AI may APPEND to its audit trail but must never read it back.
   Reading it would let a prompt-injected model learn which of its previous
   attempts were blocked and why -- an oracle for probing the guard. */
GRANT INSERT ON OBJECT::[ai].[audit_events] TO [copilot_readonly];
DENY  SELECT ON OBJECT::[ai].[audit_events] TO [copilot_readonly];
GO

/* Never readable by the AI: who may see what, and the app user registry. */
DENY SELECT ON SCHEMA::[security] TO [copilot_readonly];
GO

/* Explicit write denial across the database. db_datareader already excludes
   writes; these DENYs survive a later mistaken grant, because DENY outranks
   GRANT everywhere except for sysadmin (see the warning at the end). */
DENY INSERT, UPDATE, DELETE, ALTER, CONTROL, REFERENCES
    ON SCHEMA::[core]    TO [copilot_readonly];
DENY INSERT, UPDATE, DELETE, ALTER, CONTROL, REFERENCES
    ON SCHEMA::[billing] TO [copilot_readonly];
DENY INSERT, UPDATE, DELETE, ALTER, CONTROL, REFERENCES
    ON SCHEMA::[support] TO [copilot_readonly];
DENY INSERT, UPDATE, DELETE, ALTER, CONTROL, REFERENCES
    ON SCHEMA::[analytics] TO [copilot_readonly];
GO

/* Schema introspection: the Text-to-SQL layer retrieves table and column
   metadata to build its prompt, so it needs VIEW DEFINITION. This grants
   visibility of shapes, not of data. */
GRANT VIEW DEFINITION ON SCHEMA::[core]      TO [copilot_readonly];
GRANT VIEW DEFINITION ON SCHEMA::[billing]   TO [copilot_readonly];
GRANT VIEW DEFINITION ON SCHEMA::[support]   TO [copilot_readonly];
GRANT VIEW DEFINITION ON SCHEMA::[analytics] TO [copilot_readonly];
GO

PRINT 'Role copilot_readonly configured.';
GO

/* ===========================================================================
   PRINCIPAL PROVISIONING
   ---------------------------------------------------------------------------
   This file deliberately does NOT create a login with a password literal. A
   checked-in "temporary" password is still a credential and is commonly left
   active by mistake. Provision the principal through your secret-management
   workflow, then add only that user to the role created above.

   Azure SQL example (replace the placeholder interactively; never commit it):

       CREATE USER [copilot_app] WITH PASSWORD = '<generated secret>';
       ALTER ROLE [copilot_readonly] ADD MEMBER [copilot_app];

   SQL Server Windows-service-account example:

       CREATE LOGIN [MACHINE\copilot_svc] FROM WINDOWS;
       CREATE USER  [MACHINE\copilot_svc] FOR LOGIN [MACHINE\copilot_svc];
       ALTER ROLE   [copilot_readonly] ADD MEMBER [MACHINE\copilot_svc];
   =========================================================================== */
PRINT '';
PRINT 'Provision the application principal outside this file, then add it to copilot_readonly.';
PRINT 'Never add the application principal to db_owner, db_datareader, or sysadmin.';
GO

/* ===========================================================================
   Verification - run these AS the copilot principal, not as yourself.
   ---------------------------------------------------------------------------
       SELECT IS_SRVROLEMEMBER('sysadmin');          -- must be 0
       SELECT IS_MEMBER('db_owner');                 -- must be 0
       SELECT IS_MEMBER('db_datawriter');            -- must be 0
       SELECT TOP 1 * FROM analytics.vw_customer_360;-- must succeed
       DELETE FROM core.customers;                   -- must FAIL
       SELECT * FROM security.app_users;             -- must FAIL

   WARNING: a sysadmin bypasses every DENY above. Verifying while connected as
   sysadmin proves nothing. scripts/check_environment.py detects and reports
   this case rather than letting it pass silently.
   =========================================================================== */

PRINT '006_create_security.sql complete.';
GO
