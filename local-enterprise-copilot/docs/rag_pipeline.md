# The RAG pipeline

How a document becomes searchable, and how a question finds it. Every number
here comes from `scripts/evaluate_retrieval.py` against
`evals/document_rag.jsonl` — none of it is estimated.

---

## 1. Offline: documents to index

```
data/documents/*.md
      |
      v
  parse        MarkdownParser / PdfParser / DocxParser   (ingestion/parsers.py)
      |        YAML front matter -> DocumentMetadata
      v
  clean        whitespace, smart quotes, repeated headers
      |
      v
  sections     heading tree with breadcrumbs
      |
      v
  chunk        structure-aware, 400 tokens target        (ingestion/chunking.py)
      |
      v
  embed        qwen3-embedding:0.6b via Ollama, dim 1024 (retrieval/embedder.py)
      |
      +---> Qdrant (dense vectors + payload filters)     (retrieval/vector_store.py)
      |
      +---> BM25 (sparse, cached to disk)                (retrieval/sparse.py)
      |
      v
  manifest     index version, model, dimension, hashes   (models/documents.py)
```

Run it: `.venv\Scripts\python scripts\build_index.py`

### Why structure-aware chunking

The default approach — split every N characters — cuts clauses in half. A
retrieved chunk that reads

> "Enterprise annual plans may be refunded on a pro-rata basis within the first"

looks complete and is not. The model then invents the rest of the sentence.

This chunker instead:

- treats **sections as the unit**; a section that fits becomes one chunk
- keeps **tables whole**, repeating the header row if a table must be split, so
  a fragment is never `| 15 minutes |` with no column name
- starts a new block at each **numbered clause**
- prefixes each chunk with its **breadcrumb**
  (`[Refund and Credit Policy > 3. Annual plans]`), so the chunk is
  self-describing once retrieved and the embedding carries the section topic
- emits a **parent chunk** for any section it had to split, so retrieval can
  return a precise hit and then expand to full context

`tests/test_ingestion.py::test_refund_clause_is_not_split` asserts the specific
clause the evaluation set depends on survives intact.

### Incremental indexing

Each chunk carries `doc_content_hash`, the hash of its whole source document.
On re-index, a document whose hash is unchanged is skipped entirely.

| Run | Result |
|---|---|
| `--rebuild` | 130 chunks embedded, 12.1 s |
| re-run, nothing changed | 19/19 skipped, 0 chunks, 2.8 s |

When a document changes, its chunks are **deleted and replaced** rather than
merged. An edited document that produces fewer chunks would otherwise leave
orphans behind, and the system would keep citing text that no longer exists.

---

## 2. Online: question to evidence

```
query
  |
  +--> dense search   (Qdrant, 40 candidates, payload-filtered)
  |                                                    \
  +--> sparse search  (BM25, 40 candidates, id-filtered) > RRF fusion
  |                                                    /
  v
dedupe -> rerank (cross-encoder, top 30) -> authority preference -> MMR -> top 8
  |
  v
parent expansion -> evidence
```

Run it: `.venv\Scripts\python scripts\search.py --compare "your question"`

### Why hybrid, with evidence

Dense embeddings match **meaning**. BM25 matches **exact strings**. Neither is
sufficient alone, and the failure modes are opposite.

The clearest case in this corpus is the query `INC-2025-0042`:

| strategy | rank 1 | correct? | latency |
|---|---|---|---|
| dense | `DOC-PM-2025-0031` | **no — the wrong incident** | 2730 ms |
| sparse | `DOC-PM-2025-0042` | yes | 1 ms |
| hybrid | `DOC-PM-2025-0042` | yes | — |

Dense retrieval cannot distinguish two near-identical identifiers, because they
embed almost identically. BM25 treats them as different tokens and gets it
right instantly.

The reverse holds for multilingual queries, where BM25 has no shared vocabulary
with an English corpus and collapses.

### Measured results (42 cases, k=8)

| strategy | hit@k | MRR | recall | precision | **NDCG** | filter | mean ms |
|---|---|---|---|---|---|---|---|
| dense | 0.968 | 0.775 | 0.935 | 0.279 | 0.800 | 1.000 | 119 |
| sparse | 0.903 | 0.786 | 0.887 | 0.297 | 0.797 | 1.000 | **4** |
| hybrid | 1.000 | 0.863 | 0.968 | **0.299** | 0.871 | 1.000 | 46 |
| **reranked** | 1.000 | **0.882** | **0.984** | 0.261 | **0.886** | 1.000 | 2152 |

**Hybrid is +8.8 % NDCG over the dense baseline; reranking adds a further
+1.9 %, for +10.7 % overall — at 47x the latency.**

> Measured before reranking became adaptive. The cross-encoder is now
> skipped when dense and sparse independently agree on the top result, so
> the live ratio is roughly 20x rather than 47x. The NDCG figures in this
> table are the historical run and have not been recomputed. See
> [evaluation_report.md](evaluation_report.md) section 7.2.

### Where each method wins

| category | dense | sparse | hybrid | reranked |
|---|---|---|---|---|
| exact_code | 0.804 | **1.000** | **1.000** | 0.852 |
| multilingual | **0.877** | 0.333 | **0.877** | **0.877** |
| prompt_injection | 0.631 | **1.000** | **1.000** | **1.000** |
| simple_fact | 0.750 | **1.000** | 0.938 | 0.938 |
| paraphrase | 0.783 | 0.543 | 0.733 | **0.783** |
| version_sensitive | 0.877 | 0.810 | 0.833 | **1.000** |
| date_sensitive | 0.769 | 0.875 | 0.796 | **0.959** |
| ambiguous | 0.500 | 0.000 | 0.500 | **0.631** |
| conflict | **1.000** | **1.000** | **1.000** | 0.850 |

This table, not the average, is the argument for hybrid retrieval. Sparse is
perfect on identifiers and collapses on multilingual; dense is the reverse.

**Hybrid is not uniformly better.** On `paraphrase`, dense alone scores 0.783
against hybrid's 0.733. On `simple_fact`, sparse alone scores 1.000 against
hybrid's 0.938. Fusion trades a little peak accuracy in the categories where
one retriever is already perfect, in exchange for never collapsing the way
either does alone. That trade is worth making; pretending it is free is not.

### Does reranking earn its cost?

Measured, with the cross-encoder actually running (BAAI/bge-reranker-base, CPU,
1,236 pairs scored at 62 ms each):

**Where it clearly wins**

| category | hybrid | reranked |
|---|---|---|
| version_sensitive | 0.833 | **1.000** |
| date_sensitive | 0.796 | **0.959** |
| ambiguous | 0.500 | **0.631** |
| paraphrase | 0.733 | **0.783** |

These are the categories that need the query and the document read *together* —
exactly what a cross-encoder does and a bi-encoder cannot.

**Where it loses**

| category | hybrid | reranked |
|---|---|---|
| exact_code | **1.000** | 0.852 |
| conflict | **1.000** | 0.850 |

On exact identifiers, BM25 had already produced a perfect ranking and the
cross-encoder reshuffles it on semantic similarity, demoting the literal match.

**Overall recall rises (0.968 to 0.984) while precision falls (0.299 to
0.261)**: reranking pulls in more of the relevant documents but keeps some
weaker ones in the top 8.

**Verdict:** worth it for a question-answering workload, where nuance dominates,
but the 47x latency cost (46 ms to 2,152 ms on CPU) is real. A production
deployment should consider reranking only when the query is not a
high-confidence exact-identifier match, or move the cross-encoder to a GPU.

---

### Why Reciprocal Rank Fusion rather than blending scores

Cosine similarity is bounded and clusters in a narrow band; BM25 is unbounded
and depends on corpus statistics. Adding them requires normalisation, and
normalising over the returned window is unstable — a document's contribution
changes depending on what else happened to be retrieved.

RRF ignores scores and uses ranks:

```
score(d) = sum over retrievers of 1 / (k + rank(d))      k = 60
```

Scale-free, no per-retriever tuning, robust to one retriever being badly
calibrated. `tests/test_retrieval.py::test_is_immune_to_score_scale` asserts
that multiplying BM25 scores by 100,000 does not change the fused order.

The cost: RRF discards score magnitude. A first place won by a mile fuses the
same as one won by a hair. The reranker is what recovers that.

---

## 3. Filtering: security is part of retrieval, not an afterthought

Filters are applied **during** search, not after. Filtering afterwards still
lets forbidden chunks occupy top-k slots, so the user silently receives fewer
usable results than they asked for.

| Filter | Mechanism | Purpose |
|---|---|---|
| tenant | Qdrant payload filter | `NWC-EU` documents unreachable from `NWC-NA` |
| access group | Qdrant payload filter | finance documents unreachable by a guest |
| status | Qdrant payload filter | superseded policies excluded by default |
| doc type | Qdrant payload filter | injection-test document excluded from normal retrieval |

BM25 has no payload filtering, so the permitted chunk-id set is computed from
the vector store and applied **before** ranking (`_allowed_chunk_ids`).

Measured: **filter accuracy 1.000** across all four strategies — no permission
or version leak on any of the 42 cases. Abstention on unanswerable and
permission-denied questions: **7/7**.

---

## 4. Conflict handling

The corpus contains three deliberate conflicts:

| Conflict | Resolution mechanism |
|---|---|
| Refund policy v1.0 "non-refundable" vs v2.1 "pro-rata within 30 days" | `status` filter — superseded excluded |
| SLA v1.0 30-minute vs v2.0 15-minute P1 target | `status` filter; historical queries opt in with `current_only=False` |
| Discount ceiling 25 % (policy) vs 30 % (guidance) | `apply_authority_preference` |

The authority adjustment is deliberately small — up to 5 % — so it breaks ties
in favour of binding policy but never lets an irrelevant policy outrank a
highly relevant guidance document. That is asserted in
`test_adjustment_is_small_enough_not_to_override_relevance`.

---

## 5. Tuning knobs

All in `.env`; none hard-coded.

| Setting | Default | Effect |
|---|---|---|
| `RETRIEVAL_CHUNK_TARGET_TOKENS` | 400 | Smaller is more precise, larger keeps more context |
| `RETRIEVAL_CHUNK_OVERLAP_TOKENS` | 60 | Guards facts sitting on a chunk boundary |
| `RETRIEVAL_RRF_K` | 60 | Higher flattens the influence of top ranks |
| `RETRIEVAL_MMR_LAMBDA` | 0.7 | 1.0 is plain top-k; lower buys diversity |
| `RETRIEVAL_ENABLE_RERANKING` | true | Degrades to a no-op when unavailable |
| profile `dense_candidates` / `sparse_candidates` | 40 / 40 | Recall before reranking |
| profile `final_evidence_chunks` | 8 | What the generator actually sees |

**Change one, then measure:**

```powershell
.venv\Scripts\python scripts\evaluate_retrieval.py --k 8 --compare-baseline
```

It fails with exit code 1 if any strategy regresses by more than 0.02 NDCG
against `evals/baseline_retrieval.json`.

---

## 6. Known limitations

- **Payload indexes do nothing in embedded Qdrant.** Filters are correct but
  scan. Irrelevant at 130 chunks; use `QDRANT_MODE=server` at scale.
- **Embedded Qdrant is single-process.** An ingestion job and the Streamlit app
  cannot hold the same path at once.
- **Reranking helps on nuance and hurts on exact identifiers.** See the
  verdict section above: +1.9 % NDCG overall, but exact_code drops from
  1.000 to 0.852. The 47x latency cost is real.
- **Query rewriting, multi-query retrieval and query decomposition are not
  implemented.** Deferred deliberately: the specification says to add advanced
  techniques only after a baseline exists and only if evaluation shows value.
- **The evaluation set is small (42 cases) and was written alongside the
  system.** It is a regression guard, not an unbiased benchmark. A genuinely
  held-out set written by someone else would be stronger.
