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
   PATH A - SQL login (requires Mixed Mode)
   ---------------------------------------------------------------------------
   Change the password before running, and store it with
   `python scripts/set_secret.py` rather than in .env.
   =========================================================================== */
DECLARE @mixed_mode BIT = CASE
    WHEN CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS INT) = 0 THEN 1 ELSE 0 END;

IF @mixed_mode = 1
BEGIN
    PRINT 'Mixed Mode is ENABLED - creating the copilot_reader SQL login.';

    IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'copilot_reader')
        EXEC(N'CREATE LOGIN [copilot_reader]
                 WITH PASSWORD = N''ChangeMe_Str0ng!2026'',
                      CHECK_POLICY = ON,
                      CHECK_EXPIRATION = OFF;');

    IF DATABASE_PRINCIPAL_ID(N'copilot_reader') IS NULL
        EXEC(N'CREATE USER [copilot_reader] FOR LOGIN [copilot_reader];');

    EXEC(N'ALTER ROLE [copilot_readonly] ADD MEMBER [copilot_reader];');

    PRINT '  -> Login copilot_reader created and added to copilot_readonly.';
    PRINT '  -> CHANGE THE PASSWORD, then set in .env:';
    PRINT '       MSSQL_AUTH_MODE=sql';
    PRINT '       MSSQL_USERNAME=copilot_reader';
    PRINT '       MSSQL_PASSWORD=        (blank; use scripts/set_secret.py)';
END
ELSE
BEGIN
    PRINT '';
    PRINT '*** Mixed Mode is DISABLED on this instance. ***';
    PRINT '    The copilot_reader SQL login was NOT created.';
    PRINT '    The copilot_readonly ROLE exists and is ready for a member.';
    PRINT '';
    PRINT '    Choose one:';
    PRINT '      Path A: enable Mixed Mode (SSMS -> Properties -> Security),';
    PRINT '              restart MSSQL$SQLEXPRESS, then re-run this script.';
    PRINT '      Path B: map a dedicated Windows account (see below).';
    PRINT '';
    PRINT '    Until then the app connects as your own login. If that login is';
    PRINT '    sysadmin, ONLY the application SQL guard protects the data.';
END
GO

/* ===========================================================================
   PATH B - dedicated Windows account (no server change needed)
   ---------------------------------------------------------------------------
   Create a local Windows user, then uncomment and adjust:

       net user copilot_svc <StrongPassword> /add
       net localgroup Users copilot_svc /add

   CREATE LOGIN [MACHINE\copilot_svc] FROM WINDOWS;
   CREATE USER  [MACHINE\copilot_svc] FOR LOGIN [MACHINE\copilot_svc];
   ALTER ROLE   [copilot_readonly] ADD MEMBER [MACHINE\copilot_svc];

   Then run Streamlit as that user, keeping MSSQL_AUTH_MODE=windows.
   =========================================================================== */

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
