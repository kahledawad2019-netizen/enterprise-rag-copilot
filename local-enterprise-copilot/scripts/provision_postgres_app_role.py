"""
Provision the application login role on a managed PostgreSQL (Neon).

## Why this script exists

`copilot_app` was originally created through the Neon CLI. Roles created that
way inherit Neon's management group and come out holding **CREATEDB,
CREATEROLE, REPLICATION and BYPASSRLS**. BYPASSRLS is the fatal one: migration
009 enables and forces row-level security on fifteen tables, and a role
holding BYPASSRLS walks straight past all of it. Measured against the live
database, tenant 1 could read 284 rows belonging to other tenants, every
analytics view leaked, and DELETE, UPDATE and GRANT were accepted.

`neondb_owner` cannot repair that role. Every `ALTER ROLE copilot_app
NOBYPASSRLS` (and NOREPLICATION, NOCREATEDB, NOCREATEROLE) is refused with
`permission denied to alter role`, because the owner is not a superuser and
cannot remove an attribute it does not control.

A role created with plain SQL by `neondb_owner` comes out clean. That is the
whole fix, and it is why the application login must never be created through
the provider's CLI or console.

## Running it

    $env:COPILOT_OWNER_DSN  = <direct neondb_owner DSN>
    $env:COPILOT_APP_PASSWORD = <a freshly generated secret>
    python scripts/provision_postgres_app_role.py

Neither value is printed, logged, or written anywhere. The password is
composed into the statement as a literal because CREATE ROLE is a utility
statement and accepts no bind parameters - which also means it would appear in
`pg_stat_statements` on a server that logs utility statements. Rotate it if
that matters in your environment.

A SQL-created role's password is **not** retrievable from the Neon CLI
afterwards. That is the trade: the provider can hand back a password for a
role it manages, and a role it manages is a role that can bypass your security
model. Store the password in the backend secret manager at creation time.

## Safety

The script refuses to drop a role that owns objects, verifies the result, and
exits non-zero if the finished role holds any privileged attribute. It grants
exactly one thing: membership of the read-only group.
"""

from __future__ import annotations

import os
import sys

try:
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover - environment guard
    print("psycopg is required: pip install 'psycopg[binary]'", file=sys.stderr)
    raise SystemExit(2) from None


APP_ROLE = "copilot_app"
READONLY_GROUP = "copilot_readonly"

PRIVILEGED_ATTRIBUTES = ("rolsuper", "rolcreatedb", "rolcreaterole",
                         "rolreplication", "rolbypassrls")


def owned_objects(cursor, role: str) -> list[str]:
    """Anything the role owns. Dropping a role that owns objects would fail
    anyway; checking first turns a confusing error into a clear refusal."""
    cursor.execute(
        """
        SELECT n.nspname || '.' || c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE pg_catalog.pg_get_userbyid(c.relowner) = %s
        ORDER BY 1
        """,
        (role,),
    )
    return [row[0] for row in cursor.fetchall()]


def role_attributes(cursor, role: str) -> dict[str, bool] | None:
    cursor.execute(
        """
        SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb,
               rolcreaterole, rolreplication, rolbypassrls
        FROM pg_roles WHERE rolname = %s
        """,
        (role,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    keys = ("rolcanlogin", "rolinherit", "rolsuper", "rolcreatedb",
            "rolcreaterole", "rolreplication", "rolbypassrls")
    return dict(zip(keys, row, strict=True))


def main() -> int:
    owner_dsn = os.environ.get("COPILOT_OWNER_DSN", "").strip()
    password = os.environ.get("COPILOT_APP_PASSWORD", "").strip()

    if not owner_dsn:
        print("COPILOT_OWNER_DSN is not set.", file=sys.stderr)
        return 2
    if len(password) < 24:
        print(
            "COPILOT_APP_PASSWORD is missing or shorter than 24 characters. "
            "Generate a long random secret; this role is the only thing "
            "between the internet and the data.",
            file=sys.stderr,
        )
        return 2

    with psycopg.connect(owner_dsn, connect_timeout=30) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            before = role_attributes(cursor, APP_ROLE)

            if before is not None:
                owned = owned_objects(cursor, APP_ROLE)
                if owned:
                    print(
                        f"{APP_ROLE} owns {len(owned)} object(s) and will not be "
                        f"recreated automatically: {', '.join(owned[:5])}"
                        + (" ..." if len(owned) > 5 else ""),
                        file=sys.stderr,
                    )
                    return 1

                privileged = [k for k in PRIVILEGED_ATTRIBUTES if before[k]]
                if not privileged:
                    print(f"{APP_ROLE} already exists and holds no privileged attribute.")
                    print("Rotating its password and re-applying the grant.")
                    cursor.execute(
                        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {} INHERIT").format(
                            sql.Identifier(APP_ROLE), sql.Literal(password)
                        )
                    )
                else:
                    print(
                        f"{APP_ROLE} holds {', '.join(privileged)} and cannot be "
                        f"repaired in place - the owner is not a superuser. Recreating."
                    )
                    cursor.execute(
                        sql.SQL("DROP ROLE {}").format(sql.Identifier(APP_ROLE))
                    )
                    before = None

            if before is None:
                # INHERIT, deliberately: the role must pick up copilot_readonly's
                # privileges automatically. With NOINHERIT the application would
                # have to issue SET ROLE before every query, and forgetting once
                # is an outage.
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} WITH LOGIN PASSWORD {} INHERIT "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Identifier(APP_ROLE), sql.Literal(password))
                )
                print(f"Created {APP_ROLE} with explicit negative attributes.")

            cursor.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(READONLY_GROUP), sql.Identifier(APP_ROLE)
                )
            )
            print(f"Granted {READONLY_GROUP} to {APP_ROLE}.")

            after = role_attributes(cursor, APP_ROLE)
            if after is None:
                print("Role vanished after creation.", file=sys.stderr)
                return 1

            still_privileged = [k for k in PRIVILEGED_ATTRIBUTES if after[k]]
            print()
            print("Final attributes:")
            for key, value in after.items():
                print(f"  {key:<16}{value}")

            if still_privileged:
                print(
                    f"\nFAILED: {APP_ROLE} still holds {', '.join(still_privileged)}. "
                    f"Do not deploy with this role.",
                    file=sys.stderr,
                )
                return 1
            if not after["rolcanlogin"]:
                print(f"\nFAILED: {APP_ROLE} cannot log in.", file=sys.stderr)
                return 1
            if not after["rolinherit"]:
                print(
                    f"\nFAILED: {APP_ROLE} is NOINHERIT and will not pick up "
                    f"{READONLY_GROUP} without SET ROLE.",
                    file=sys.stderr,
                )
                return 1

    print(f"\n{APP_ROLE} provisioned with least privilege.")
    print("Store the password in the backend secret manager now - it cannot be "
          "read back from the provider.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
