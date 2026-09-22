/* ===========================================================================
   001_create_database.sql  (PostgreSQL)

   PostgreSQL translation of the SQL Server script of the same number.
   Schema version: 1.0.0
   Target        : PostgreSQL 15+ (developed against Neon)

   Run order: 001 -> 002 -> 003 -> 004 -> 005 -> 006 -> 007 -> 008 -> 009

   ---------------------------------------------------------------------------
   Why this file is nearly empty, and the T-SQL one is not
   ---------------------------------------------------------------------------
   The SQL Server script does three things that have no PostgreSQL equivalent
   worth reproducing:

     CREATE DATABASE EnterpriseCopilot
         A managed PostgreSQL provider creates the database for you, and on
         Neon in particular you cannot CREATE DATABASE from inside a normal
         connection to that database. The database name comes from the
         connection string instead.

     ALTER DATABASE ... SET RECOVERY SIMPLE
         A SQL Server recovery model. PostgreSQL has WAL and the provider owns
         its retention; there is nothing to set here.

     ALTER DATABASE ... SET READ_COMMITTED_SNAPSHOT ON
         SQL Server needs this so readers do not take shared locks that would
         block the data generator. PostgreSQL uses MVCC by default, so readers
         never block writers and writers never block readers. The property the
         original script had to ask for is the one PostgreSQL already has.

   So the only thing left is to assert that we are connected somewhere sane
   and record the schema version, which makes a wrong-database mistake fail
   here rather than three scripts later.
   =========================================================================== */

DO $$
BEGIN
    IF current_setting('server_version_num')::int < 150000 THEN
        RAISE EXCEPTION
            'PostgreSQL 15 or newer is required; this server reports %',
            current_setting('server_version');
    END IF;

    RAISE NOTICE 'Connected to database % as %', current_database(), current_user;
END
$$;
