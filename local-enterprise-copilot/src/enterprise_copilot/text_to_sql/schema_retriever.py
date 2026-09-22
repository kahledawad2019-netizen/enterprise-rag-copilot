"""
Schema, glossary and example retrieval for Text-to-SQL.

## Why not just send the whole schema

The database has 25 tables, 6 views and roughly 200 columns. Rendered as DDL
that is several thousand tokens on **every** question, which costs latency,
crowds out the evidence that matters, and measurably degrades accuracy: a model
shown fifty tables picks the wrong one far more often than a model shown five.

So the relevant subset is retrieved per question, the same way document chunks
are. Three things are retrieved and all three matter:

1. **Schema** — the tables and views the question plausibly touches, with their
   columns and the foreign keys that join them.
2. **Business definitions** — from `ai.business_glossary`. This is the part
   most Text-to-SQL systems omit and it is why they get business questions
   wrong. The schema says `mrr_amount DECIMAL(19,4)`; only the glossary says
   that trials are excluded and annual contracts are already normalised.
3. **Approved examples** — human-verified question/SQL pairs. Few-shot examples
   are the single highest-leverage input to generation quality.

## How relevance is scored

Hybrid, for the same reason document retrieval is hybrid: lexical matching
catches a question that names a column, and embeddings catch a question that
does not. "Who are our biggest customers?" shares no words with
`vw_customer_360`, and "how many SLA breaches" shares an obvious one with
`sla_breaches`. Neither method alone covers both.

The catalog is read from `INFORMATION_SCHEMA` once and cached to disk with its
embeddings, so this costs nothing per question beyond embedding the question.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..database.connection import raw_connection
from ..retrieval.sparse import tokenize

log = logging.getLogger(__name__)

# Schemas the model is allowed to know about at all. `ai` and `security` are
# excluded here as well as in the guard, so the model is never even aware of
# the audit trail or the permission tables.
VISIBLE_SCHEMAS = ("analytics", "core", "billing", "support")

# Internal helpers the model should never be offered. vw_month_spine is a
# calendar used by the revenue views; selecting it directly is never the right
# answer and it has no tenant_id, so it also trips the isolation check.
HIDDEN_OBJECTS = frozenset({"analytics.vw_month_spine"})


@dataclass
class TableInfo:
    """One table or view, with everything needed to render usable DDL."""

    schema: str
    name: str
    object_type: str  # BASE TABLE | VIEW
    columns: list[dict[str, Any]] = field(default_factory=list)
    foreign_keys: list[dict[str, str]] = field(default_factory=list)
    row_estimate: int = 0

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def is_view(self) -> bool:
        return self.object_type == "VIEW"

    def searchable_text(self) -> str:
        """What relevance is scored against.

        Column names are included because a question often names a column
        without naming its table ("how many seats", "which region").
        """
        column_names = " ".join(c["name"] for c in self.columns)
        kind = "curated analytics view" if self.is_view else "base table"
        return f"{self.schema} {self.name} {kind} {column_names}"

    def to_ddl(self, *, max_columns: int = 40, dialect: str = "tsql") -> str:
        def quote(identifier: str) -> str:
            return identifier if dialect == "postgres" else f"[{identifier}]"

        lines = [
            f"CREATE {'VIEW' if self.is_view else 'TABLE'} "
            f"{quote(self.schema)}.{quote(self.name)} ("
        ]
        for column in self.columns[:max_columns]:
            nullable = "NULL" if column["nullable"] else "NOT NULL"
            lines.append(f"  {quote(column['name'])} {column['type']} {nullable},")
        if len(self.columns) > max_columns:
            lines.append(f"  -- ... {len(self.columns) - max_columns} more columns")
        if lines[-1].endswith(","):
            lines[-1] = lines[-1].rstrip(",")
        lines.append(");")
        return "\n".join(lines)


@dataclass
class SQLContext:
    """Everything assembled for one Text-to-SQL prompt."""

    question: str
    tables: list[TableInfo] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    glossary: list[dict[str, str]] = field(default_factory=list)
    examples: list[dict[str, str]] = field(default_factory=list)
    tenant_id: int | None = None
    dialect: str = "tsql"

    def table_names(self) -> list[str]:
        return [t.qualified for t in self.tables]

    def render_schema(self) -> str:
        return "\n\n".join(t.to_ddl(dialect=self.dialect) for t in self.tables)

    def render_relationships(self) -> str:
        return "\n".join(f"- {r}" for r in self.relationships) if self.relationships else "(none)"

    def render_glossary(self) -> str:
        if not self.glossary:
            return "(no business definitions matched this question)"
        blocks = []
        for term in self.glossary:
            blocks.append(
                f"TERM: {term['term']} (v{term['version']})\n"
                f"  definition: {term['definition']}\n"
                f"  how to compute: {term['sql_guidance']}\n"
                f"  excludes: {term.get('known_exclusions') or 'nothing stated'}"
            )
        return "\n\n".join(blocks)

    def render_examples(self) -> str:
        if not self.examples:
            return "(no approved examples matched this question)"
        return "\n\n".join(f"Q: {e['question']}\nSQL:\n{e['sql_text']}" for e in self.examples)


class SchemaRetriever:
    def __init__(self, settings: Settings | None = None, *, embedder=None) -> None:
        self.settings = settings or get_settings()
        self._embedder = embedder
        self._catalog: list[TableInfo] | None = None
        self._relationships: list[str] | None = None
        self._table_vectors: dict[str, list[float]] | None = None

    @property
    def cache_path(self) -> Path:
        return self.settings.manifests_dir / f"schema_catalog_{self.settings.sql_dialect}.json"

    # -- catalog -----------------------------------------------------------
    def load_catalog(self, *, refresh: bool = False) -> list[TableInfo]:
        """Read the schema from INFORMATION_SCHEMA, caching to disk."""
        if self._catalog is not None and not refresh:
            return self._catalog

        if self.cache_path.exists() and not refresh:
            try:
                payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
                self._catalog = [TableInfo(**t) for t in payload["tables"]]
                self._relationships = payload["relationships"]
                self._table_vectors = payload.get("vectors")
                log.info("Schema catalog loaded from cache (%d objects)", len(self._catalog))
                return self._catalog
            except Exception as exc:
                log.warning("Schema cache unreadable (%s); rebuilding", exc)

        self._catalog, self._relationships = self._read_from_database()
        self._table_vectors = None
        self._write_cache()
        return self._catalog

    def _read_from_database(self) -> tuple[list[TableInfo], list[str]]:
        marker = "%s" if self.settings.database_backend == "postgresql" else "?"
        placeholders = ", ".join(marker for _ in VISIBLE_SCHEMAS)
        tables: dict[str, TableInfo] = {}

        with raw_connection(self.settings) as conn:
            cursor = conn.cursor()
            catalog_sql = f"""
                SELECT t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_TYPE,
                       c.COLUMN_NAME, c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH,
                       c.NUMERIC_PRECISION, c.NUMERIC_SCALE, c.IS_NULLABLE,
                       c.ORDINAL_POSITION
                FROM INFORMATION_SCHEMA.TABLES t
                JOIN INFORMATION_SCHEMA.COLUMNS c
                  ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
                WHERE t.TABLE_SCHEMA IN ({placeholders})
                ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME, c.ORDINAL_POSITION
                """
            if self.settings.database_backend == "postgresql":
                cursor.execute(catalog_sql, VISIBLE_SCHEMAS)
            else:
                cursor.execute(catalog_sql, *VISIBLE_SCHEMAS)
            for row in cursor.fetchall():
                (schema, name, kind, column, dtype, length, precision, scale, nullable, _) = row
                key = f"{schema}.{name}"
                if key.lower() in HIDDEN_OBJECTS:
                    continue
                if key not in tables:
                    tables[key] = TableInfo(schema=schema, name=name, object_type=kind)

                rendered = dtype
                if length is not None:
                    rendered = f"{dtype}({'MAX' if length == -1 else length})"
                elif dtype in ("decimal", "numeric") and precision is not None:
                    rendered = f"{dtype}({precision},{scale or 0})"

                tables[key].columns.append(
                    {
                        "name": column,
                        "type": rendered,
                        "nullable": nullable == "YES",
                    }
                )

            if self.settings.database_backend == "postgresql":
                cursor.execute(
                    """
                    SELECT tc.table_schema, tc.table_name, kcu.column_name,
                           ccu.table_schema, ccu.table_name, ccu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON kcu.constraint_schema = tc.constraint_schema
                     AND kcu.constraint_name = tc.constraint_name
                    JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_schema = tc.constraint_schema
                     AND ccu.constraint_name = tc.constraint_name
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_schema IN ('core','billing','support','analytics')
                    """
                )
            else:
                cursor.execute(
                    """
                SELECT sp.name, tp.name, cp.name, sr.name, tr.name, cr.name
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
            relationships = []
            for ps, pt, pc, rs, rt, rc in cursor.fetchall():
                relationships.append(f"{ps}.{pt}.{pc} = {rs}.{rt}.{rc}")
                key = f"{ps}.{pt}"
                if key in tables:
                    tables[key].foreign_keys.append({"column": pc, "references": f"{rs}.{rt}.{rc}"})

        log.info(
            "Schema catalog read: %d objects, %d relationships", len(tables), len(relationships)
        )
        return list(tables.values()), relationships

    def _write_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tables": [
                {
                    "schema": t.schema,
                    "name": t.name,
                    "object_type": t.object_type,
                    "columns": t.columns,
                    "foreign_keys": t.foreign_keys,
                    "row_estimate": t.row_estimate,
                }
                for t in (self._catalog or [])
            ],
            "relationships": self._relationships or [],
            "vectors": self._table_vectors,
        }
        self.cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # -- relevance ---------------------------------------------------------
    def _ensure_embedder(self):
        """Create the embedder if it does not exist yet.

        Kept separate from vector building because the vectors may come from
        the disk cache while the embedder has never been constructed. Without
        this, `select_tables` called `self._embedder.embed_query` on None,
        caught the AttributeError, and silently fell back to lexical-only
        matching - so semantic schema linking was disabled on every run with a
        warm cache, which is the normal case.
        """
        if self._embedder is None:
            from ..retrieval.embedder import OllamaEmbedder

            self._embedder = OllamaEmbedder(self.settings)
        return self._embedder

    def _ensure_vectors(self) -> dict[str, list[float]] | None:
        """Embed each table's searchable text once, then cache it."""
        if self._table_vectors:
            return self._table_vectors
        try:
            self._ensure_embedder()
            catalog = self.load_catalog()
            vectors = self._embedder.embed_documents([t.searchable_text() for t in catalog])
            self._table_vectors = {t.qualified: v for t, v in zip(catalog, vectors, strict=True)}
            self._write_cache()
            return self._table_vectors
        except Exception as exc:
            log.warning("Schema embeddings unavailable (%s); lexical matching only", exc)
            return None

    def select_tables(self, question: str, *, limit: int = 6) -> list[TableInfo]:
        """Pick the tables the question plausibly needs.

        Analytics views are given a deliberate advantage. They encode the
        business definitions, so steering generation towards them is the single
        most effective way to prevent the model inventing its own MRR formula.
        """
        catalog = self.load_catalog()
        question_tokens = set(tokenize(question))

        lexical: dict[str, float] = {}
        for table in catalog:
            table_tokens = set(tokenize(table.searchable_text()))
            overlap = len(question_tokens & table_tokens)
            lexical[table.qualified] = overlap / max(len(question_tokens), 1)

        semantic: dict[str, float] = {}
        vectors = self._ensure_vectors()
        if vectors:
            try:
                query_vector = self._ensure_embedder().embed_query(question)
                for table in catalog:
                    vector = vectors.get(table.qualified)
                    if vector:
                        semantic[table.qualified] = _cosine(query_vector, vector)
            except Exception as exc:
                log.warning("Question embedding failed (%s); lexical matching only", exc)

        scored: list[tuple[float, TableInfo]] = []
        for table in catalog:
            score = 0.45 * lexical.get(table.qualified, 0.0) + 0.55 * semantic.get(
                table.qualified, 0.0
            )
            if table.is_view:
                score += 0.15  # prefer the curated surface
            scored.append((score, table))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        selected = [table for _, table in scored[:limit]]

        # Pull in directly-related tables so a join the model needs is never
        # missing from the schema it was shown.
        selected_names = {t.qualified for t in selected}
        for table in list(selected):
            for fk in table.foreign_keys:
                target = ".".join(fk["references"].split(".")[:2])
                if target in selected_names:
                    continue
                extra = next((t for t in catalog if t.qualified == target), None)
                if extra is not None and len(selected) < limit + 3:
                    selected.append(extra)
                    selected_names.add(target)

        return selected

    def relationships_for(self, tables: list[TableInfo]) -> list[str]:
        names = {t.qualified for t in tables}
        relationships = self._relationships or []
        return [
            r
            for r in relationships
            if any(f"{n}." in r for n in names)
        ]

    # -- glossary and examples ---------------------------------------------
    def select_glossary(self, question: str, *, limit: int = 4) -> list[dict[str, str]]:
        """Business definitions relevant to the question.

        Only `is_current = 1` rows are returned. The superseded MRR definition
        exists in the table deliberately, and feeding it to the model would
        reintroduce exactly the error it was retired for.
        """
        question_tokens = set(tokenize(question))
        with raw_connection(self.settings) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT term, definition, sql_guidance, version, known_exclusions,
                       related_tables
                FROM ai.business_glossary
                WHERE is_current = {current_literal}
                """
                .format(
                    current_literal=(
                        "TRUE" if self.settings.database_backend == "postgresql" else "1"
                    )
                )
            )
            rows = cursor.fetchall()

        scored: list[tuple[float, dict[str, str]]] = []
        for term, definition, guidance, version, exclusions, related in rows:
            haystack = set(tokenize(f"{term} {definition} {related or ''}"))
            overlap = len(question_tokens & haystack) / max(len(question_tokens), 1)
            # An exact mention of the term is a much stronger signal than
            # incidental word overlap with a long definition.
            if term.lower() in question.lower():
                overlap += 1.0
            if overlap > 0:
                scored.append(
                    (
                        overlap,
                        {
                            "term": term,
                            "definition": definition,
                            "sql_guidance": guidance,
                            "version": version,
                            "known_exclusions": exclusions or "",
                        },
                    )
                )

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def select_examples(self, question: str, *, limit: int = 3) -> list[dict[str, str]]:
        """Human-verified question/SQL pairs most similar to this question."""
        question_tokens = set(tokenize(question))
        with raw_connection(self.settings) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT question, sql_text, category, tables_used
                FROM ai.approved_sql_examples
                WHERE is_active = {active_literal}
                """
                .format(
                    active_literal=(
                        "TRUE" if self.settings.database_backend == "postgresql" else "1"
                    )
                )
            )
            rows = cursor.fetchall()

        scored: list[tuple[float, dict[str, str]]] = []
        for example_question, sql_text, category, tables_used in rows:
            haystack = set(tokenize(f"{example_question} {category} {tables_used or ''}"))
            overlap = len(question_tokens & haystack) / max(len(question_tokens | haystack), 1)
            scored.append(
                (
                    overlap,
                    {
                        "question": example_question,
                        "sql_text": sql_text,
                        "category": category,
                    },
                )
            )

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for score, item in scored[:limit] if score > 0]

    # -- assembly ----------------------------------------------------------
    def build_context(
        self, question: str, *, tenant_id: int | None = None, table_limit: int = 6
    ) -> SQLContext:
        tables = self.select_tables(question, limit=table_limit)
        return SQLContext(
            question=question,
            tables=tables,
            relationships=self.relationships_for(tables),
            glossary=self.select_glossary(question),
            examples=self.select_examples(question),
            tenant_id=tenant_id,
            dialect=self.settings.sql_dialect,
        )


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


__all__ = ["VISIBLE_SCHEMAS", "SQLContext", "SchemaRetriever", "TableInfo"]
