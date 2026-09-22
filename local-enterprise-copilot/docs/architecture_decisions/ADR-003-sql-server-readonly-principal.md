# ADR-003: Read-only database principal on a Windows-Authentication-only server

- **Status:** Accepted, with one action left to the machine owner
- **Date:** 2026-09-18
- **Deciders:** Lead engineer

## Context

The system executes SQL written by a language model. That SQL is untrusted
input. The design calls for defence in depth:

1. a SQL validator that parses and refuses anything that is not a single read;
2. a **database principal that cannot write even if the validator is bypassed**.

Layer 2 is the real security boundary. Layer 1 is application code: a bug, a
refactor, or a cleverly shaped model output can defeat it. A `db_datareader`
principal cannot be defeated by any of those.

Inspecting the target instance produced a finding that blocks the obvious
implementation:

```
Edition        : Express Edition (64-bit)
Version        : 17.0.1000.7          (SQL Server 2025)
Collation      : SQL_Latin1_General_CP1_CI_AS
IntegratedOnly : 1
Login          : Ambitious\ambit | sysadmin: 1
```

`IsIntegratedSecurityOnly = 1` means the instance is in **Windows
Authentication mode only**. `CREATE LOGIN ... WITH PASSWORD` will fail. The
standard recipe — create a `copilot_reader` SQL login, grant it
`db_datareader`, connect the app as that login — cannot be applied as-is.

Compounding it: the only available identity is `Ambitious\ambit`, which is
**sysadmin**. A sysadmin bypasses every database-level `DENY`. Running the app
under this identity means layer 2 does not exist at all, no matter what the SQL
scripts grant or deny.

## Decision

Ship `sql/006_create_security.sql` supporting **two paths**, default to
documenting Path A, and make the application detect and report which one is
actually in force.

### Path A — enable Mixed Mode, use a SQL login (recommended)

A one-time change by the machine owner:

1. SSMS → right-click the instance → Properties → Security →
   *SQL Server and Windows Authentication mode*.
2. Restart the `MSSQL$SQLEXPRESS` service.
3. Run `sql/006_create_security.sql`, which creates the `copilot_reader` login,
   maps it into `EnterpriseCopilot`, grants `db_datareader` plus
   `VIEW DEFINITION`, and explicitly `DENY`s every write verb.
4. Set `MSSQL_AUTH_MODE=sql`, `MSSQL_USERNAME=copilot_reader`, and store the
   password with `python scripts/set_secret.py` (Windows Credential Manager,
   DPAPI-encrypted) rather than in `.env`.

Equivalently, `ALTER LOGIN` via T-SQL plus a registry `LoginMode = 2` change
and a service restart.

### Path B — no server change, dedicated Windows account

Create a local Windows user, create a Windows login for it, grant the same
read-only role membership, and run Streamlit as that user. No Mixed Mode
needed. Heavier operationally, so it is documented as the alternative rather
than the default.

### In all cases — the application tells the truth about its own posture

`scripts/check_environment.py` and the Streamlit *System Configuration* page
both run:

```sql
SELECT IS_SRVROLEMEMBER('sysadmin'), IS_MEMBER('db_owner'), IS_MEMBER('db_datawriter');
```

and report, prominently, when the connected principal can write. The README,
`docs/security.md`, and the UI all state plainly that with a sysadmin
connection **only layer 1 is protecting the data**.

**This ADR does not change the server's authentication mode.** Switching an
instance to Mixed Mode alters a security setting on a machine the project does
not own and requires a service restart that would interrupt anything else using
it. That is the owner's decision, and it is recorded here as the one
outstanding action rather than performed silently.

## Consequences

**Positive**
- The security model is explicit, and its current weakness is visible in the
  tooling instead of hidden behind an assumption.
- Both paths are scripted; neither is a manual click-through.
- Layer 1 (sqlglot validation, single-statement, schema allow-list, blocked
  columns, tenant predicate, row cap, timeout) is implemented and tested
  regardless, so the system is never unprotected.

**Negative**
- Until Path A or B is completed, the deployment runs with a privileged
  connection and defence in depth is incomplete. This is a documented, visible
  limitation, not a silent one.
- Express Edition adds its own limits (10 GB per database, ~1.4 GB buffer pool,
  4 cores). The synthetic dataset is sized to stay far under 10 GB.

## Verification

- `scripts/check_environment.py` fails loudly (not just warns) when
  `SECURITY_REQUIRE_READONLY_PRINCIPAL=true` and the principal can write.
- `tests/test_sql_guard.py` proves layer 1 blocks writes, batches,
  `SELECT ... INTO`, `EXEC`, unapproved schemas and cross-tenant reads.
- `tests/test_readonly_principal.py` is marked `integration` and asserts that a
  write attempt is refused by the *server*; it is skipped with a clear reason
  when Path A/B has not been completed.
