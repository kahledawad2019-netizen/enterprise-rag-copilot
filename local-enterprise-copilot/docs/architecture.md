# Architecture

## The shape of the problem

Two knowledge sources that cannot be merged:

| | Documents | Database |
|---|---|---|
| Content | what the company **says** | what the data **shows** |
| Shape | prose, versioned, superseded | tables, exact, current |
| Retrieval | embeddings + keywords | generated SQL |
| Failure | retrieves the wrong passage | writes the wrong query |

A question like *"compare the contractual response time with the actual
response time for customer X"* needs both, and needs them kept apart in the
answer. That constraint drives the whole design.

## Online path

```
question
  |
  v
[1] ROUTER ............ rules first, then a model
  |                     destructive intent -> refuse, before any SQL exists
  |                     ambiguous -> clarify
  |                     otherwise -> documents / SQL / both
  |
  +---------------------------+
  |                           |
  v                           v
[2] DOCUMENT RETRIEVAL   [3] TEXT-TO-SQL
  dense (Qdrant)           schema subset (not the whole schema)
  sparse (BM25)            + business glossary
  RRF fusion               + approved examples
  cross-encoder rerank     -> T-SQL
  MMR diversity            -> sqlglot AST guard
  parent expansion         -> read-only execution + audit
  |                           |
  +---------------------------+
  |
  v
[4] EVIDENCE PACKAGE ... [D1..] documents   [S1..] data   (kept distinct)
  |
  v
[5] GENERATION ......... local llama3.1, evidence only
  |
  v
[6] CITATION VALIDATION  every [D1] must exist in the package
  |
  v
answer + sources + trace
```

## Offline path

```
data/documents/*.md
  -> parse (markdown / pdf / docx)
  -> clean (whitespace, smart quotes, repeated headers)
  -> sections (heading tree with breadcrumbs)
  -> chunk (structure-aware, clause- and table-safe)
  -> embed (qwen3-embedding, dimension detected)
  -> Qdrant + BM25 + manifest

SQL Server
  -> INFORMATION_SCHEMA + sys.foreign_keys
  -> schema catalog (ai and security schemas excluded)
  -> embed table descriptions
  -> cached to disk
```

## Module map

| Package | Responsibility |
|---|---|
| `config/` | typed settings, hardware profiles. Nothing reads `os.environ` directly |
| `models/` | the contracts: `Chunk`, `Evidence`, `Answer`, `IndexManifest` |
| `ingestion/` | parsers, cleaning, chunking, index lifecycle |
| `retrieval/` | embedder, Qdrant, BM25, fusion, the hybrid pipeline |
| `reranking/` | cross-encoder, with a no-op fallback |
| `generation/` | prompts, answering, citation validation |
| `routing/` | router and the orchestrator that assembles everything |
| `text_to_sql/` | provider interface, Vanna, native, schema retrieval |
| `database/` | connection, read-only runner, synthetic data |
| `security/` | the sqlglot SQL guard |
| `observability/` | tracing, structured logging, redaction |
| `evaluation/` | retrieval metrics |

`app/` renders. It never decides what is safe, relevant or retrievable.

## Decisions worth knowing

**No agent framework.** Every stage must be independently testable and
measurable. A framework owning routing and tool selection makes "why was this
answer wrong?" much harder to answer, and the specification asks for the core
logic to stay understandable.

**Two Text-to-SQL providers.** Vanna is the specified default; the native
provider is the fallback and the reference. They share one guard, one runner,
one schema retriever and one glossary, so comparing them measures *generation*
and nothing else. See ADR-002 for why nothing safety-critical depends on Vanna.

**Validation after generation, never during.** The model is not asked to check
its own citations or its own SQL. Both are verified afterwards by code.

**Filters run during search.** Filtering afterwards lets forbidden chunks
occupy top-k slots, silently shrinking the result set for the user.
