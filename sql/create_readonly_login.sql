/* ===========================================================================
   Least-privilege login for the RAG agent.

   The SQL executed against this database is written by an LLM, so it is
   untrusted input. The application-side guardrail in src/security.py refuses
   anything that is not a read -- but a guardrail you can patch out is not a
   security boundary. The database must refuse writes on its own.

   Run this ONCE, in SQL Server Management Studio or sqlcmd, as an admin.
   Replace <StrongPasswordHere> and the database name first.
   =========================================================================== */

USE [master];
GO

-- 1. Server-level login ------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'rag_reader')
BEGIN
    CREATE LOGIN [rag_reader]
        WITH PASSWORD      = N'<StrongPasswordHere>',
             CHECK_POLICY  = ON,          -- enforce Windows password policy
             CHECK_EXPIRATION = ON;
END
GO

-- 2. Database user -----------------------------------------------------------
USE [BikeStores];   -- <-- change to your database
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'rag_reader')
BEGIN
    CREATE USER [rag_reader] FOR LOGIN [rag_reader];
END
GO

-- 3. Read-only membership ----------------------------------------------------
ALTER ROLE [db_datareader] ADD MEMBER [rag_reader];
GO

-- 4. Explicitly deny every write path ---------------------------------------
--    db_datareader already excludes writes; these DENYs make it explicit and
--    survive someone later adding the user to a broader role by mistake.
DENY INSERT, UPDATE, DELETE, ALTER, EXECUTE, CONTROL
    ON DATABASE::[BikeStores] TO [rag_reader];
GO

-- 5. Allow schema introspection (needed by src/train.py) ---------------------
GRANT VIEW DEFINITION      ON DATABASE::[BikeStores] TO [rag_reader];
GRANT VIEW DATABASE STATE  ON DATABASE::[BikeStores] TO [rag_reader];
GO

/* ---------------------------------------------------------------------------
   Verify (run while connected AS rag_reader):

       SELECT IS_MEMBER('db_owner'), IS_MEMBER('db_datawriter');   -- expect 0, 0
       SELECT TOP 1 * FROM sales.orders;                            -- works
       DELETE FROM sales.orders;                                    -- must fail

   Then in .env:
       MSSQL_AUTH_MODE=sql
       MSSQL_USERNAME=rag_reader
       MSSQL_PASSWORD=          <- leave blank, and run:
                                   python scripts\set_secret.py
   --------------------------------------------------------------------------- */
