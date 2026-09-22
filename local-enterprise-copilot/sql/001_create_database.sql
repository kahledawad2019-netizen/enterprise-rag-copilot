/* ===========================================================================
   001_create_database.sql
   Local Enterprise Intelligence Copilot - database creation

   Schema version: 1.0.0
   Target        : SQL Server 2019+ (developed against 2025 Express 17.0.1000.7)

   Run order: 001 -> 002 -> 003 -> 004 -> 005 -> 006 -> 007 -> 008
   In SSMS, run each script with SQLCMD mode OFF; the scripts use plain T-SQL.

   Idempotent: safe to re-run. It will not drop an existing database.
   =========================================================================== */

USE [master];
GO

IF DB_ID(N'EnterpriseCopilot') IS NULL
BEGIN
    PRINT 'Creating database EnterpriseCopilot ...';
    CREATE DATABASE [EnterpriseCopilot];
END
ELSE
    PRINT 'Database EnterpriseCopilot already exists - skipping CREATE.';
GO

/* Recovery model SIMPLE keeps the log small for a local analytics workload.
   Change this for a production deployment that needs point-in-time restore. */
ALTER DATABASE [EnterpriseCopilot] SET RECOVERY SIMPLE;
GO

/* READ_COMMITTED_SNAPSHOT lets the copilot's read-only queries run without
   taking shared locks that would block the data generator. This matters
   because the app is expected to query while ingestion jobs run. */
ALTER DATABASE [EnterpriseCopilot] SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE;
GO

ALTER DATABASE [EnterpriseCopilot] SET AUTO_CREATE_STATISTICS ON;
ALTER DATABASE [EnterpriseCopilot] SET AUTO_UPDATE_STATISTICS ON;
GO

PRINT '001_create_database.sql complete.';
GO
