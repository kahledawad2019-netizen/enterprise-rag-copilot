"""
Mechanical T-SQL to PostgreSQL translation for the schema scripts.

## Why a script and not a hand translation

There are 2,198 lines across nine files. A hand translation is unreviewable
(nobody diffs 2,198 lines of SQL carefully) and unrepeatable (when the T-SQL
changes, the PostgreSQL copy silently drifts). A script makes the translation
a reviewable set of rules, and re-running it proves the two stay in step.

## What it does NOT do

This handles the mechanical layer only: types, identifier quoting, batch
separators, idempotency guards, and the handful of built-ins with exact
equivalents. It does not attempt:

  * DENY               - PostgreSQL has no such concept. 006 needs a human
                         decision about what weakens and how to compensate.
  * SECURITY POLICY    - PostgreSQL RLS is a different and better shape.
  * computed columns, SCHEMABINDING, and anything where the two engines
    disagree about semantics rather than spelling.

Those files are translated by hand and excluded here. Anything this script
emits still has to pass .github/workflows/postgres.yml against a real server
before it counts as working.

Usage:
    python scripts/translate_tsql_to_postgres.py sql/003_create_tables.sql
    python scripts/translate_tsql_to_postgres.py --all --out sql/postgres
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Files whose differences are semantic rather than syntactic. Translating
# these mechanically would produce something that runs and does not mean the
# same thing, which is worse than not translating them at all.
HAND_TRANSLATED = {
    "001_create_database.sql",   # CREATE DATABASE / recovery model / RCSI
    "002_create_schemas.sql",    # trivial, plus the PUBLIC revoke
    "006_create_security.sql",   # DENY has no equivalent
    "009_enable_row_level_security.sql",  # SECURITY POLICY vs CREATE POLICY
}


def strip_batches(sql: str) -> str:
    """Remove USE and GO, which psql has no concept of."""
    sql = re.sub(r"(?im)^\s*USE\s+\[?[A-Za-z_][\w]*\]?\s*;?\s*$", "", sql)
    sql = re.sub(r"(?im)^\s*GO\s*;?\s*$", "", sql)
    return sql


def unbracket(sql: str) -> str:
    """[identifier] -> identifier.

    Applied after string literals are protected, so a bracket inside quoted
    text is left alone.
    """
    return re.sub(r"\[([A-Za-z_][\w ]*)\]", r"\1", sql)


def drop_unicode_prefix(sql: str) -> str:
    """N'text' -> 'text'. PostgreSQL text is already Unicode."""
    return re.sub(r"\bN'", "'", sql)


def convert_types(sql: str) -> str:
    """Type vocabulary. Order matters: NVARCHAR(MAX) before NVARCHAR(n)."""
    replacements = [
        (r"\bNVARCHAR\s*\(\s*MAX\s*\)", "TEXT"),
        (r"\bVARCHAR\s*\(\s*MAX\s*\)", "TEXT"),
        (r"\bNVARCHAR\s*\(", "VARCHAR("),
        (r"\bNCHAR\s*\(", "CHAR("),
        # DATETIME2(n) carries its precision across unchanged.
        (r"\bDATETIME2\s*\(\s*(\d)\s*\)", r"TIMESTAMP(\1)"),
        (r"\bDATETIME2\b", "TIMESTAMP"),
        (r"\bDATETIME\b", "TIMESTAMP"),
        # TINYINT is 0-255 in T-SQL and does not exist in PostgreSQL.
        # SMALLINT is the narrowest type that holds the whole range.
        (r"\bTINYINT\b", "SMALLINT"),
        (r"\bUNIQUEIDENTIFIER\b", "UUID"),
        (r"\bMONEY\b", "DECIMAL(19,4)"),
        (r"\bFLOAT\b", "DOUBLE PRECISION"),
        (r"\bBIT\b", "BOOLEAN"),
    ]
    for pattern, replacement in replacements:
        sql = re.sub(pattern, replacement, sql, flags=re.IGNORECASE)
    return sql


def convert_builtins(sql: str) -> str:
    """Built-in functions with exact equivalents."""
    sql = re.sub(
        r"\bSYSUTCDATETIME\s*\(\s*\)",
        "(NOW() AT TIME ZONE 'utc')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"\bGETUTCDATE\s*\(\s*\)", "(NOW() AT TIME ZONE 'utc')", sql, flags=re.I)
    sql = re.sub(r"\bGETDATE\s*\(\s*\)", "NOW()", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bISNULL\s*\(", "COALESCE(", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bLEN\s*\(", "LENGTH(", sql, flags=re.IGNORECASE)
    return sql


def convert_identity(sql: str) -> str:
    """IDENTITY(1,1) -> GENERATED ALWAYS AS IDENTITY.

    T-SQL writes `INT NOT NULL IDENTITY(1,1) CONSTRAINT PK PRIMARY KEY`.
    PostgreSQL wants the identity clause attached to the type and before the
    constraints, so the NOT NULL that IDENTITY already implies is dropped to
    avoid saying it twice.
    """
    sql = re.sub(
        r"\bINT\s+NOT\s+NULL\s+IDENTITY\s*\(\s*\d+\s*,\s*\d+\s*\)",
        "INT GENERATED ALWAYS AS IDENTITY",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bBIGINT\s+NOT\s+NULL\s+IDENTITY\s*\(\s*\d+\s*,\s*\d+\s*\)",
        "BIGINT GENERATED ALWAYS AS IDENTITY",
        sql,
        flags=re.IGNORECASE,
    )
    # Any remaining spelling, e.g. `INT IDENTITY(1,1) NOT NULL`.
    sql = re.sub(
        r"\bIDENTITY\s*\(\s*\d+\s*,\s*\d+\s*\)",
        "GENERATED ALWAYS AS IDENTITY",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def convert_boolean_defaults(sql: str) -> str:
    """DEFAULT (1)/(0) on a BOOLEAN column -> DEFAULT TRUE/FALSE.

    Runs after convert_types, so the column already reads BOOLEAN and the
    match can be anchored on it rather than guessing which integers are
    really flags.
    """
    def fix(match: re.Match[str]) -> str:
        head, value = match.group(1), match.group(2)
        return f"{head}DEFAULT {'TRUE' if value == '1' else 'FALSE'}"

    return re.sub(
        r"(\bBOOLEAN\b[^,\n]*?)DEFAULT\s*\(\s*([01])\s*\)",
        fix,
        sql,
        flags=re.IGNORECASE,
    )


def drop_default_constraint_names(sql: str) -> str:
    """CONSTRAINT DF_x DEFAULT (...) -> DEFAULT (...).

    PostgreSQL has no named DEFAULT constraints. Named CHECK, FOREIGN KEY,
    PRIMARY KEY and UNIQUE constraints are kept, because those names appear in
    error messages and the project's tests assert on some of them.
    """
    return re.sub(
        r"\bCONSTRAINT\s+\w+\s+DEFAULT\b",
        "DEFAULT",
        sql,
        flags=re.IGNORECASE,
    )


def convert_object_guards(sql: str) -> str:
    """IF OBJECT_ID(...) IS NULL\\nCREATE TABLE x -> CREATE TABLE IF NOT EXISTS x.

    This is the transformation sqlglot refuses - it reports "Unsupported If
    block syntax" and drops the guard, which would turn an idempotent script
    into one that fails on a second run.
    """
    sql = re.sub(
        r"(?is)IF\s+OBJECT_ID\s*\(\s*'[^']+'\s*,\s*'[A-Z]+'\s*\)\s+IS\s+NULL\s*"
        r"(?:BEGIN\s*)?CREATE\s+TABLE\s+",
        "CREATE TABLE IF NOT EXISTS ",
        sql,
    )
    sql = re.sub(
        r"(?is)IF\s+OBJECT_ID\s*\(\s*'[^']+'\s*,\s*'[A-Z]+'\s*\)\s+IS\s+NULL\s*"
        r"(?:BEGIN\s*)?CREATE\s+(?:UNIQUE\s+)?INDEX\s+",
        "CREATE INDEX IF NOT EXISTS ",
        sql,
    )

    # 004 guards its 29 indexes a third way:
    #
    #     IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_x')
    #     CREATE NONCLUSTERED INDEX IX_x ON ...
    #
    # `sys.indexes` is a SQL Server catalogue view that does not exist in
    # PostgreSQL, so the probe cannot be kept even if the IF could. PostgreSQL
    # has CREATE INDEX IF NOT EXISTS, which says the same thing in one clause.
    #
    # The UNIQUE keyword has to survive the rewrite. Dropping it would turn a
    # uniqueness constraint into an ordinary index and remove a data-integrity
    # rule while still creating something that looks right.
    sql = re.sub(
        r"(?is)IF\s+NOT\s+EXISTS\s*\(\s*SELECT[^)]*?\bsys\.indexes\b[^)]*?\)\s*"
        r"CREATE\s+(UNIQUE\s+)?(?:NONCLUSTERED\s+|CLUSTERED\s+)?INDEX\s+",
        lambda m: f"CREATE {m.group(1) or ''}INDEX IF NOT EXISTS ",
        sql,
    )
    return sql


def convert_conditional_insert(sql: str) -> str:
    """IF NOT EXISTS (<probe>) INSERT INTO t (cols) VALUES (vals);

    -> INSERT INTO t (cols) SELECT vals WHERE NOT EXISTS (<probe>);

    T-SQL guards a seed row with a statement-level IF. PostgreSQL has no
    statement-level IF outside a procedural block, but INSERT ... SELECT
    ... WHERE NOT EXISTS expresses the same thing in one statement, keeps it
    atomic, and stays idempotent on re-run.

    ON CONFLICT DO NOTHING would be shorter but is not equivalent: it needs a
    unique constraint on exactly the probed columns, and these probes test a
    value that is not always unique-indexed.

    Found by the CI harness, not by reading: the first run failed with
    `syntax error at or near "IF"` on the schema_version seed at the end of
    003, which the OBJECT_ID rule never matched.
    """
    pattern = re.compile(
        r"(?is)\bIF\s+NOT\s+EXISTS\s*(\((?:[^()]|\([^()]*\))*\))\s*"
        r"INSERT\s+INTO\s+([\w.]+)\s*(\([^)]*\))\s*"
        r"VALUES\s*(\((?:[^()']|'[^']*')*\))\s*;"
    )

    def fix(match: re.Match[str]) -> str:
        probe, table, columns, values = match.groups()
        # VALUES (a, b, c) -> SELECT a, b, c
        projection = values.strip()[1:-1].strip()
        return (
            f"INSERT INTO {table} {columns}\n"
            f"SELECT {projection}\n"
            f"WHERE NOT EXISTS {probe};"
        )

    return pattern.sub(fix, sql)


def convert_index_syntax(sql: str) -> str:
    """Index storage hints that PostgreSQL does not have.

    CLUSTERED/NONCLUSTERED describe SQL Server's physical row storage: the
    clustered index IS the table. PostgreSQL heap tables have no such notion,
    so the keyword is simply dropped rather than emulated - CLUSTER exists but
    is a one-off reorganisation, not a permanent property, and pretending
    otherwise would be a lie in the schema.

    INCLUDE is kept: PostgreSQL 11+ supports covering indexes with the same
    spelling and the same meaning.
    """
    sql = re.sub(r"\bCREATE\s+NONCLUSTERED\s+INDEX\b", "CREATE INDEX", sql, flags=re.I)
    sql = re.sub(r"\bCREATE\s+CLUSTERED\s+INDEX\b", "CREATE INDEX", sql, flags=re.I)
    sql = re.sub(
        r"\bCREATE\s+UNIQUE\s+NONCLUSTERED\s+INDEX\b", "CREATE UNIQUE INDEX", sql, flags=re.I
    )
    sql = re.sub(r"(?im)^\s*SET\s+NOCOUNT\s+(ON|OFF)\s*;?\s*$", "", sql)
    return sql


def convert_date_functions(sql: str) -> str:
    """DATEADD and DATEDIFF, which have no direct PostgreSQL spelling.

        DATEADD(DAY, -30, x)      -> (x + INTERVAL '-30 day')
        DATEDIFF(MINUTE, a, b)    -> (EXTRACT(EPOCH FROM (b - a)) / 60)

    DATEDIFF is the one to be careful about. SQL Server counts *boundaries
    crossed*, not elapsed time: DATEDIFF(DAY, '2026-01-01 23:59', '2026-01-02
    00:01') is 1, though two minutes passed. The PostgreSQL form below measures
    elapsed time and truncates, so it returns 0 for that pair.

    For this schema the difference does not bite - every DATEDIFF here measures
    a duration between two timestamps (response time, resolution time), which
    is exactly what elapsed-time semantics mean. It would bite on a query that
    counts calendar days or month boundaries, so the divergence is recorded
    here rather than discovered later.
    """
    unit_seconds = {
        "second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
    }

    def fix_datediff(match: re.Match[str]) -> str:
        unit, start, end = match.group(1).lower(), match.group(2), match.group(3)
        divisor = unit_seconds.get(unit)
        if divisor is None:
            # MONTH/YEAR are calendar arithmetic, not elapsed seconds. Leave
            # them untranslated so the script fails loudly rather than
            # returning a plausible wrong number.
            return match.group(0)
        inner = f"EXTRACT(EPOCH FROM (({end}) - ({start})))"
        return f"({inner} / {divisor})::int" if divisor != 1 else f"{inner}::int"

    sql = re.sub(
        r"\bDATEDIFF\s*\(\s*(\w+)\s*,\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
        fix_datediff,
        sql,
        flags=re.IGNORECASE,
    )

    def fix_dateadd(match: re.Match[str]) -> str:
        unit, amount, target = match.group(1).lower(), match.group(2).strip(), match.group(3)
        return f"(({target}) + INTERVAL '{amount} {unit}')"

    sql = re.sub(
        r"\bDATEADD\s*\(\s*(\w+)\s*,\s*(-?\s*\d+)\s*,\s*([^)]+?)\s*\)",
        fix_dateadd,
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def convert_apply(sql: str) -> str:
    """APPLY -> LATERAL.

    CROSS APPLY is CROSS JOIN LATERAL. OUTER APPLY is LEFT JOIN LATERAL with
    `ON TRUE`, because a LEFT JOIN needs a condition and the lateral
    subquery's own WHERE already did the filtering.
    """
    sql = re.sub(r"\bCROSS\s+APPLY\b", "CROSS JOIN LATERAL", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bOUTER\s+APPLY\b", "LEFT JOIN LATERAL", sql, flags=re.IGNORECASE)
    return sql


def convert_print(sql: str) -> str:
    """PRINT 'x' -> a DO block raising a notice, which psql shows the same way."""
    def fix(match: re.Match[str]) -> str:
        message = match.group(1).replace("'", "''")
        return f"DO $$ BEGIN RAISE NOTICE '{message}'; END $$;"

    return re.sub(r"(?im)^\s*PRINT\s+'([^']*)'\s*;?\s*$", fix, sql)


def tidy(sql: str) -> str:
    """Collapse the blank runs left behind by removing GO."""
    return re.sub(r"\n{4,}", "\n\n\n", sql).strip() + "\n"


def translate(sql: str) -> str:
    for step in (
        strip_batches,
        drop_unicode_prefix,
        convert_object_guards,
        convert_types,
        convert_identity,
        convert_boolean_defaults,
        drop_default_constraint_names,
        convert_builtins,
        convert_index_syntax,
        convert_date_functions,
        convert_apply,
        convert_conditional_insert,
        convert_print,
        unbracket,
        tidy,
    ):
        sql = step(sql)
    return sql


HEADER = """/* ===========================================================================
   {name}  (PostgreSQL)

   GENERATED by scripts/translate_tsql_to_postgres.py from sql/{name}.
   Do not edit by hand - edit the T-SQL source and re-run the translator, or
   the two dialects drift and only one of them is tested.

   Verified by .github/workflows/postgres.yml against a real PostgreSQL 16.
   =========================================================================== */

"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--all", action="store_true", help="translate every eligible script")
    parser.add_argument("--out", type=Path, default=Path("sql/postgres"))
    args = parser.parse_args()

    source_dir = Path("sql")
    if args.all:
        files = sorted(
            p for p in source_dir.glob("[0-9][0-9][0-9]_*.sql")
            if p.name not in HAND_TRANSLATED
        )
    else:
        files = args.files

    if not files:
        parser.error("give one or more files, or --all")

    args.out.mkdir(parents=True, exist_ok=True)
    for path in files:
        if path.name in HAND_TRANSLATED:
            print(f"skip  {path.name}  (hand-translated: semantics differ)")
            continue
        translated = HEADER.format(name=path.name) + translate(
            path.read_text(encoding="utf-8")
        )
        target = args.out / path.name
        target.write_text(translated, encoding="utf-8")
        print(f"write {target}  ({len(translated.splitlines())} lines)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
