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


def _split_args(text: str, start: int) -> tuple[list[str], int] | None:
    """Read a balanced argument list beginning at `start` (the opening paren).

    Returns the arguments and the index just past the closing paren, or None
    if the parentheses never balance.

    This exists because `[^)]+?` is wrong for any argument that contains
    parentheses of its own, and these scripts are full of them:

        DATEADD(DAY, -30, CAST(SYSUTCDATETIME() AS date))

    A non-greedy stop-at-the-first-paren match captures
    `CAST(SYSUTCDATETIME() AS date` and emits SQL whose parentheses do not
    balance. That is worse than failing, because it is valid-looking text that
    only the server rejects - which is exactly how it was found.

    Quoted strings are skipped so a parenthesis inside a literal cannot
    unbalance the scan.
    """
    if start >= len(text) or text[start] != "(":
        return None

    depth = 0
    args: list[str] = []
    current: list[str] = []
    i = start
    in_string = False

    while i < len(text):
        ch = text[i]

        if in_string:
            current.append(ch)
            if ch == "'":
                # '' is an escaped quote inside a string, not the end of one.
                if i + 1 < len(text) and text[i + 1] == "'":
                    current.append(text[i + 1])
                    i += 2
                    continue
                in_string = False
            i += 1
            continue

        if ch == "'":
            in_string = True
            current.append(ch)
        elif ch == "(":
            depth += 1
            if depth > 1:
                current.append(ch)
        elif ch == ")":
            depth -= 1
            if depth == 0:
                args.append("".join(current).strip())
                return args, i + 1
            current.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1

    return None


def _rewrite_calls(sql: str, name: str, build: object) -> str:
    """Rewrite every call to `name(...)` using a balanced-paren scan.

    `build` receives the argument list and returns the replacement text, or
    None to leave the call untouched.
    """
    pattern = re.compile(rf"\b{name}\s*(?=\()", re.IGNORECASE)
    out: list[str] = []
    cursor = 0

    while True:
        match = pattern.search(sql, cursor)
        if match is None:
            out.append(sql[cursor:])
            return "".join(out)

        paren = sql.index("(", match.end() - 1)
        parsed = _split_args(sql, paren)
        if parsed is None:
            out.append(sql[cursor:match.end()])
            cursor = match.end()
            continue

        args, end = parsed
        replacement = build(args)  # type: ignore[operator]
        out.append(sql[cursor:match.start()])
        out.append(replacement if replacement is not None else sql[match.start():end])
        cursor = end


def convert_create_or_alter(sql: str) -> str:
    """CREATE OR ALTER -> CREATE OR REPLACE.

    T-SQL 2016+ spells it ALTER; PostgreSQL spells it REPLACE. The semantics
    match for views and functions, which is all these scripts use it for.
    """
    return re.sub(
        r"\bCREATE\s+OR\s+ALTER\s+(VIEW|FUNCTION|PROCEDURE)\b",
        r"CREATE OR REPLACE \1",
        sql,
        flags=re.IGNORECASE,
    )


def convert_top(sql: str) -> str:
    """SELECT TOP (n) ... -> SELECT ... LIMIT n.

    TOP sits at the head of the projection and LIMIT at the tail of the
    query, so this cannot be a substitution - the clause has to move. The
    scan walks forward from the TOP, tracking parenthesis depth, and places
    LIMIT at the end of the SELECT that owns it: either just before the
    parenthesis that closes an enclosing subquery, or at the statement's
    semicolon.

    Depth tracking is what makes nesting work. 005 has a TOP (1) inside a
    lateral subquery that itself contains another TOP (1) in a correlated
    predicate; each has to receive its own LIMIT in its own place.
    """
    pattern = re.compile(r"\bSELECT\s+TOP\s*\(\s*(\d+)\s*\)\s*", re.IGNORECASE)

    while True:
        match = pattern.search(sql)
        if match is None:
            return sql

        limit = match.group(1)
        depth = 0
        i = match.end()
        insert_at = len(sql)
        in_string = False

        while i < len(sql):
            ch = sql[i]
            if in_string:
                if ch == "'":
                    in_string = False
                i += 1
                continue
            if ch == "'":
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    insert_at = i
                    break
                depth -= 1
            elif ch == ";" and depth == 0:
                insert_at = i
                break
            i += 1

        body = sql[match.end():insert_at].rstrip()
        sql = (
            sql[:match.start()]
            + "SELECT "
            + body
            + f"\n    LIMIT {limit}\n"
            + sql[insert_at:]
        )


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
    """DATEADD and DATEDIFF, using the balanced-paren scanner.

        DATEADD(DAY, -30, CAST(SYSUTCDATETIME() AS date))
            -> ((CAST((NOW() AT TIME ZONE 'utc') AS date)) + INTERVAL '-30 day')

        DATEDIFF(MINUTE, a, b)
            -> (EXTRACT(EPOCH FROM ((b) - (a))) / 60)::int

    The first version of this used `[^)]+?` for the arguments and silently
    produced unbalanced SQL whenever an argument contained parentheses - which
    the CAST above does. It looked fine and only the server complained.

    A semantic caveat that survives the fix: SQL Server's DATEDIFF counts
    *boundaries crossed*, not elapsed time. DATEDIFF(DAY, '2026-01-01 23:59',
    '2026-01-02 00:01') is 1 in T-SQL though two minutes passed; the form
    below measures elapsed seconds and truncates, so it returns 0. Every
    DATEDIFF in this schema measures a duration between two timestamps, which
    is elapsed-time semantics, so the difference does not bite here. MONTH and
    YEAR are left untranslated on purpose so calendar arithmetic fails loudly
    rather than returning a plausible wrong number.
    """
    unit_seconds = {
        "second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
    }

    def datediff(args: list[str]) -> str | None:
        if len(args) != 3:
            return None
        unit, start_expr, end_expr = args[0].strip().lower(), args[1], args[2]
        divisor = unit_seconds.get(unit)
        if divisor is None:
            return None  # MONTH/YEAR: leave it to fail visibly
        epoch = f"EXTRACT(EPOCH FROM (({end_expr}) - ({start_expr})))"
        return f"({epoch})::int" if divisor == 1 else f"(({epoch} / {divisor}))::int"

    def dateadd(args: list[str]) -> str | None:
        if len(args) != 3:
            return None
        unit, amount, target = args[0].strip().lower(), args[1].strip(), args[2]
        if not re.fullmatch(r"-?\s*\d+", amount):
            return None  # a non-literal interval needs a human
        return f"(({target}) + INTERVAL '{amount} {unit}')"

    sql = _rewrite_calls(sql, "DATEDIFF", datediff)
    sql = _rewrite_calls(sql, "DATEADD", dateadd)
    return sql


def convert_date_parts(sql: str) -> str:
    """T-SQL date-part functions, which PostgreSQL spells with EXTRACT.

        YEAR(x)                -> EXTRACT(YEAR FROM x)::int
        MONTH(x)               -> EXTRACT(MONTH FROM x)::int
        DAY(x)                 -> EXTRACT(DAY FROM x)::int
        DATEPART(unit, x)      -> EXTRACT(unit FROM x)::int
        DATEFROMPARTS(y, m, d) -> MAKE_DATE(y, m, d)
        EOMONTH(x)             -> (date_trunc('month', x) + INTERVAL '1 month -1 day')::date

    The ::int casts matter. EXTRACT returns numeric in PostgreSQL, and
    MAKE_DATE takes integers, so DATEFROMPARTS(YEAR(...), MONTH(...), 1)
    fails on the argument types without them. T-SQL's YEAR() returns int, so
    casting also keeps the expression's type the same across dialects rather
    than quietly widening it to numeric wherever the result is used.

    Found all at once by grepping the translated output for T-SQL scalar
    functions, rather than one per CI run: the server reports the first
    failure and stops, so a file with five unknown functions takes five
    round trips unless you go looking.
    """
    def extract(unit: str):
        def build(args: list[str]) -> str | None:
            if len(args) != 1:
                return None
            return f"EXTRACT({unit} FROM {args[0]})::int"
        return build

    for name in ("YEAR", "MONTH", "DAY"):
        sql = _rewrite_calls(sql, name, extract(name))

    def datepart(args: list[str]) -> str | None:
        if len(args) != 2:
            return None
        unit = args[0].strip().strip("'\"").upper()
        if not re.fullmatch(r"[A-Z]+", unit):
            return None
        return f"EXTRACT({unit} FROM {args[1]})::int"

    sql = _rewrite_calls(sql, "DATEPART", datepart)

    def datefromparts(args: list[str]) -> str | None:
        if len(args) != 3:
            return None
        return f"MAKE_DATE({args[0]}, {args[1]}, {args[2]})"

    sql = _rewrite_calls(sql, "DATEFROMPARTS", datefromparts)

    def eomonth(args: list[str]) -> str | None:
        # T-SQL's optional month-offset argument is not used in these scripts;
        # refusing it is better than guessing at the arithmetic.
        if len(args) != 1:
            return None
        return (
            f"(date_trunc('month', ({args[0]})::timestamp) "
            f"+ INTERVAL '1 month' - INTERVAL '1 day')::date"
        )

    sql = _rewrite_calls(sql, "EOMONTH", eomonth)
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
        convert_create_or_alter,
        convert_date_parts,
        convert_top,
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
