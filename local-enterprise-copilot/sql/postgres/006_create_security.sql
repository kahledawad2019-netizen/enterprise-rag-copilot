/* ===========================================================================
   006_create_security.sql  (PostgreSQL)

   HAND-TRANSLATED from sql/006_create_security.sql.

   PostgreSQL has no DENY that overrides later grants. The compensating
   controls are: a narrowly granted NOLOGIN group role, no ownership or role-
   creation attributes, database-enforced RLS in 009, and executable CI tests.
   The deployed login is provisioned by the platform and made a member of this
   role; credentials never belong in a migration.
   =========================================================================== */

DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'copilot_readonly') THEN
        CREATE ROLE copilot_readonly NOLOGIN;
    END IF;
END
$role$;

/* Managed PostgreSQL providers such as Neon do not let project owners alter
   superuser-only attributes, even when the requested value is the safe
   negative form (NOSUPERUSER / NOREPLICATION / NOBYPASSRLS).  New roles
   already default to those values.  Apply the owner-manageable attributes,
   then fail closed if a pre-existing role has any privileged attribute. */
ALTER ROLE copilot_readonly WITH NOCREATEDB NOCREATEROLE NOLOGIN;

DO $role_safety$
DECLARE
    role_is_unsafe boolean;
BEGIN
    SELECT rolsuper OR rolreplication OR rolbypassrls
      INTO role_is_unsafe
      FROM pg_roles
     WHERE rolname = 'copilot_readonly';

    IF role_is_unsafe IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION
            'copilot_readonly must not be SUPERUSER, REPLICATION, or BYPASSRLS';
    END IF;
END
$role_safety$;

/* Reset this role's object privileges before applying the allowlist. */
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA core, billing, support, analytics, ai, security
    FROM copilot_readonly;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA core, billing, support, analytics, ai, security
    FROM copilot_readonly;
REVOKE ALL PRIVILEGES ON SCHEMA core, billing, support, analytics, ai, security
    FROM copilot_readonly;

GRANT USAGE ON SCHEMA core, billing, support, analytics, ai TO copilot_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA core, billing, support, analytics
    TO copilot_readonly;

/* Prompt inputs are readable; the audit trail is append-only to this role. */
GRANT SELECT ON TABLE ai.business_glossary, ai.approved_sql_examples, ai.schema_version
    TO copilot_readonly;
GRANT INSERT ON TABLE ai.audit_events TO copilot_readonly;
GRANT USAGE ON SEQUENCE ai.audit_events_audit_id_seq TO copilot_readonly;

/* Keep future business tables readable without broadening the sensitive
   schemas. PostgreSQL default privileges apply to objects created later by
   the migration owner executing this script. */
ALTER DEFAULT PRIVILEGES IN SCHEMA core, billing, support, analytics
    GRANT SELECT ON TABLES TO copilot_readonly;

DO $$ BEGIN RAISE NOTICE 'PostgreSQL role copilot_readonly configured.'; END $$;

/* Provisioning example -- run through the hosting secret workflow, never here:

       CREATE ROLE copilot_app LOGIN PASSWORD '<generated secret>'
           NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
       GRANT copilot_readonly TO copilot_app;

   The absence of PostgreSQL DENY means ownership, superuser, BYPASSRLS, or an
   accidental later GRANT can defeat this allowlist. CI verifies the effective
   role, and production must repeat those checks using the real app login.
*/
