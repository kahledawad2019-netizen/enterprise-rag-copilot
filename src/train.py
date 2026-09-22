"""
Train the RAG corpus.

Vanna's retrieval quality is driven almost entirely by what you put in here.
Three kinds of material are stored in Chroma:

  1. DDL          - the shape of the schema (tables, columns, types, keys)
  2. Documentation - business meaning: what a column really represents,
                     which joins are correct, how "active customer" is defined
  3. Question/SQL pairs - worked examples; by far the highest-value signal

Run:  python -m src.train
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from pathlib import Path

from .settings import PROJECT_ROOT, get_settings
from .vanna_agent import build_vanna

log = logging.getLogger(__name__)


@contextlib.contextmanager
def quiet(enabled: bool = True):
    """Swallow Vanna's raw print() calls during bulk training."""
    if not enabled:
        yield
        return
    with contextlib.redirect_stdout(io.StringIO()):
        yield

TRAINING_DIR = PROJECT_ROOT / "training"
DOCS_DIR = TRAINING_DIR / "documentation"
PAIRS_FILE = TRAINING_DIR / "question_sql_pairs.json"

# Schemas that are never useful to the model.
SYSTEM_SCHEMAS = ("sys", "INFORMATION_SCHEMA", "guest", "db_owner", "db_accessadmin")


def build_ddl_statements(vn) -> list[str]:
    """Reconstruct a CREATE TABLE statement per table from INFORMATION_SCHEMA."""
    cols = vn.run_sql_unguarded(
        """
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE,
               CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
               IS_NULLABLE, ORDINAL_POSITION
        FROM INFORMATION_SCHEMA.COLUMNS
        ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """
    )

    statements: list[str] = []
    for (schema, table), group in cols.groupby(["TABLE_SCHEMA", "TABLE_NAME"], sort=False):
        if schema in SYSTEM_SCHEMAS:
            continue
        lines = []
        for _, r in group.iterrows():
            dtype = str(r["DATA_TYPE"])
            length = r["CHARACTER_MAXIMUM_LENGTH"]
            precision = r["NUMERIC_PRECISION"]
            scale = r["NUMERIC_SCALE"]
            if length is not None and str(length) not in ("nan", "None"):
                size = "MAX" if int(length) == -1 else int(length)
                dtype = f"{dtype}({size})"
            elif dtype in ("decimal", "numeric") and str(precision) not in ("nan", "None"):
                dtype = f"{dtype}({int(precision)},{int(scale or 0)})"
            null = "NULL" if str(r["IS_NULLABLE"]).upper() == "YES" else "NOT NULL"
            lines.append(f"  [{r['COLUMN_NAME']}] {dtype} {null}")
        statements.append(
            f"CREATE TABLE [{schema}].[{table}] (\n" + ",\n".join(lines) + "\n);"
        )
    return statements


def build_relationship_docs(vn) -> list[str]:
    """Describe every foreign key in words so the model joins tables correctly."""
    fks = vn.run_sql_unguarded(
        """
        SELECT
            fk.name                        AS fk_name,
            sp.name                        AS parent_schema,
            tp.name                        AS parent_table,
            cp.name                        AS parent_column,
            sr.name                        AS ref_schema,
            tr.name                        AS ref_table,
            cr.name                        AS ref_column
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        JOIN sys.tables  tp ON tp.object_id = fkc.parent_object_id
        JOIN sys.schemas sp ON sp.schema_id = tp.schema_id
        JOIN sys.columns cp ON cp.object_id = tp.object_id AND cp.column_id = fkc.parent_column_id
        JOIN sys.tables  tr ON tr.object_id = fkc.referenced_object_id
        JOIN sys.schemas sr ON sr.schema_id = tr.schema_id
        JOIN sys.columns cr ON cr.object_id = tr.object_id AND cr.column_id = fkc.referenced_column_id
        """
    )
    return [
        f"To join [{r.parent_schema}].[{r.parent_table}] to "
        f"[{r.ref_schema}].[{r.ref_table}], use "
        f"[{r.parent_schema}].[{r.parent_table}].[{r.parent_column}] = "
        f"[{r.ref_schema}].[{r.ref_table}].[{r.ref_column}] "
        f"(foreign key {r.fk_name})."
        for r in fks.itertuples()
    ]


def train_from_files(vn) -> int:
    """Load hand-written documentation (.md/.txt) and question/SQL pairs."""
    count = 0

    if DOCS_DIR.exists():
        for path in sorted(DOCS_DIR.glob("*")):
            if path.suffix.lower() in (".md", ".txt") and path.stat().st_size:
                vn.train(documentation=path.read_text(encoding="utf-8"))
                log.info("Documentation trained: %s", path.name)
                count += 1

    if PAIRS_FILE.exists() and PAIRS_FILE.stat().st_size:
        pairs = json.loads(PAIRS_FILE.read_text(encoding="utf-8"))
        for pair in pairs:
            question, sql = pair.get("question"), pair.get("sql")
            if question and sql:
                vn.train(question=question, sql=sql)
                count += 1
        log.info("Question/SQL pairs trained: %s", len(pairs))

    return count


def main() -> None:
    settings = get_settings()
    vn = build_vanna(settings)

    log.info("Reading schema from [%s] ...", settings.mssql_database)
    ddl = build_ddl_statements(vn)
    with quiet(not settings.verbose):
        for statement in ddl:
            vn.train(ddl=statement)
    log.info("Tables trained: %s", len(ddl))

    rels = build_relationship_docs(vn)
    with quiet(not settings.verbose):
        for doc in rels:
            vn.train(documentation=doc)
    log.info("Foreign-key relationships trained: %s", len(rels))

    with quiet(not settings.verbose):
        extra = train_from_files(vn)
    log.info("Extra documentation / examples trained: %s", extra)

    total = len(vn.get_training_data())
    log.info("Done. Training corpus now holds %s items.", total)
    print(f"\nTraining complete: {len(ddl)} tables, {len(rels)} relationships, "
          f"{extra} manual items -> {total} total items in {settings.chroma_path}")


if __name__ == "__main__":
    main()
