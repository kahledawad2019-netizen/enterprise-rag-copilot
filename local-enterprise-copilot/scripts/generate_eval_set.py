r"""
Generate a held-out evaluation set that the author did not write.

    .venv\Scripts\python scripts\generate_eval_set.py
    .venv\Scripts\python scripts\generate_eval_set.py --per-doc 4 --output evals/document_rag_holdout.jsonl

## Why this exists

`evals/document_rag.jsonl` was written by the same person who wrote the
documents and the chunker. That makes it a **development set**: useful as a
regression guard, but not evidence of generalisation. Tuning against it and
then quoting its numbers as a benchmark is the single most common way RAG
projects overstate their quality.

This script builds a **test set** instead:

* Chunks are sampled **uniformly across every document**, not chosen by a human
  who remembers which passages retrieval handles well.
* Questions are written by the local LLM from the chunk text alone. The author
  never sees the passage before the question exists.
* Ground truth is **mechanical**: the document the chunk came from. No human
  decides what counts as relevant.
* Generated questions are filtered by rules, never hand-picked. Curating the
  output by hand would reintroduce exactly the bias this is removing.

## The bias that remains, and how it is measured

LLM-generated questions tend to reuse the source passage's vocabulary. That
**leakage inflates BM25** in particular, because lexical overlap is what BM25
scores. It is not eliminated here, so it is measured: every case records the
Jacquard overlap between the question and its source chunk, and the evaluation
report breaks results down by low, medium and high overlap.

A question generated from a chunk may also be answerable from another document.
That is counted as a miss, which makes these numbers a **lower bound** on true
retrieval quality rather than an optimistic one.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "evals" / "document_rag_holdout.jsonl"

GENERATION_PROMPT = """You are writing questions to test a company's internal search system.

Below is one passage from an internal document at a fictional B2B SaaS company \
called Northwind Cloud.

Write {count} distinct questions that an employee or customer might ask, where \
this passage contains the answer.

Rules:
- Each question must be answerable from the passage alone.
- Write the question the way a real person would ask it, NOT by copying \
sentences from the passage.
- Rephrase in your own words. Avoid reusing distinctive phrases where a natural \
alternative exists.
- Never refer to "the passage", "this document", "the text" or "above". The \
person asking has not seen it.
- Keep each question under 25 words.
- Vary the style: some direct, some conversational.

PASSAGE:
---
{text}
---

Reply with ONLY a JSON object of this exact shape:
{{"questions": ["...", "..."]}}"""

# Negative controls: plausible enterprise questions about topics this corpus
# does not cover. Retrieval must find nothing relevant, which is what makes
# abstention measurable. Without these the held-out set only measures the easy
# direction - finding things that exist.
NEGATIVE_CONTROLS = [
    "What is the company policy on employee parking?",
    "How many people work in the Brazil office?",
    "What is the maternity leave entitlement?",
    "Which health insurance provider does the company use?",
    "What is the travel expense limit for international flights?",
    "When is the next company all-hands meeting?",
    "What laptop model do new engineers receive?",
    "What is the dress code in the London office?",
    "How do I book a meeting room?",
    "What is the company's carbon offset programme?",
    "Who is the head of human resources?",
    "What is the policy on pets in the office?",
]

# Questions that leak the evaluation setup, or that no user would ever type.
REJECT_PATTERNS = [
    re.compile(r"\b(this|the)\s+(passage|document|text|excerpt|section|snippet)\b", re.I),
    re.compile(r"\babove\b", re.I),
    re.compile(r"\baccording to the\b", re.I),
    re.compile(r"^\s*$"),
]


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


def lexical_overlap(question: str, passage: str) -> float:
    """Jaccard overlap. The higher this is, the more BM25 is being helped."""
    q, p = tokens(question), tokens(passage)
    if not q or not p:
        return 0.0
    return len(q & p) / len(q)  # share of question words present in the passage


def acceptable(question: str) -> tuple[bool, str]:
    question = question.strip()
    if len(question) < 15:
        return False, "too short"
    if len(question.split()) > 30:
        return False, "too long"
    if not question.endswith("?"):
        return False, "not a question"
    for pattern in REJECT_PATTERNS:
        if pattern.search(question):
            return False, f"mentions the evaluation setup ({pattern.pattern[:30]})"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a held-out evaluation set")
    parser.add_argument("--per-doc", type=int, default=3, help="questions per document")
    parser.add_argument(
        "--chunks-per-doc", type=int, default=2, help="passages sampled per document"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=None, help="defaults to COPILOT_SEED")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--negatives",
        type=int,
        default=0,
        help="append N unanswerable control questions, so abstention is measurable",
    )
    parser.add_argument(
        "--model",
        help="model that WRITES the questions. Using a different family from the "
        "one being evaluated reduces self-preference bias, where a model "
        "favours phrasing its own family retrieves well.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s | %(message)s",
    )

    import ollama

    from enterprise_copilot.config import get_settings
    from enterprise_copilot.ingestion.chunking import ChunkingConfig, StructureAwareChunker
    from enterprise_copilot.ingestion.parsers import ParserRegistry

    settings = get_settings()
    seed = args.seed if args.seed is not None else settings.random_seed
    rng = random.Random(seed)

    registry = ParserRegistry()
    chunker = StructureAwareChunker(ChunkingConfig.from_settings(settings))
    client = ollama.Client(settings.ollama.host, timeout=settings.ollama.timeout_seconds)
    generator_model = args.model or settings.chat_model

    paths = sorted(settings.documents_dir.glob("*.md"))
    print("=" * 84)
    print("  Held-out evaluation set generation")
    print(f"  documents : {len(paths)}")
    print(f"  generator : {generator_model}  (questions are NOT author-written)")
    print(
        f"  evaluating: {settings.chat_model} pipeline"
        + (
            "   [INDEPENDENT GENERATOR]"
            if generator_model != settings.chat_model
            else "   [same family - self-preference possible]"
        )
    )
    print(f"  seed      : {seed}")
    print("=" * 84)

    cases: list[dict] = []
    rejected: dict[str, int] = defaultdict(int)
    case_number = 0

    for path in paths:
        document = registry.parse(path)
        metadata = document.metadata

        # The injection-test document is excluded from normal retrieval, so
        # generating answerable questions from it would be testing the wrong
        # thing. Its behaviour is covered by the security evaluation instead.
        if metadata.doc_type == "security_test":
            continue

        chunks = [c for c in chunker.chunk_document(document) if c.token_estimate >= 80]
        if not chunks:
            continue

        # Uniform sampling: the author does not choose which passages are tested.
        sampled = rng.sample(chunks, min(args.chunks_per_doc, len(chunks)))

        for chunk in sampled:
            # Strip the breadcrumb so the model cannot simply echo the heading.
            passage = re.sub(r"^\[.*?\]\s*", "", chunk.text, flags=re.DOTALL).strip()

            try:
                response = client.chat(
                    model=generator_model,
                    messages=[
                        {
                            "role": "user",
                            "content": GENERATION_PROMPT.format(
                                count=args.per_doc, text=passage[:2500]
                            ),
                        }
                    ],
                    format="json",
                    options={"temperature": 0.7, "num_ctx": settings.profile.chat_context_tokens},
                )
                payload = json.loads(response["message"]["content"])
                questions = payload.get("questions", [])
            except Exception as exc:
                logging.warning("Generation failed for %s: %s", metadata.doc_id, exc)
                rejected["generation error"] += 1
                continue

            for question in questions:
                if not isinstance(question, str):
                    rejected["not a string"] += 1
                    continue
                ok, reason = acceptable(question)
                if not ok:
                    rejected[reason] += 1
                    continue

                case_number += 1
                overlap = lexical_overlap(question, passage)
                cases.append(
                    {
                        "id": f"HOLD-{case_number:03d}",
                        "category": "generated",
                        "query": question.strip(),
                        "relevant_docs": [metadata.doc_id],
                        "source_chunk_id": chunk.chunk_id,
                        "source_section": chunk.section_path,
                        "doc_type": str(metadata.doc_type),
                        "lexical_overlap": round(overlap, 3),
                        "overlap_band": (
                            "high" if overlap >= 0.6 else "medium" if overlap >= 0.35 else "low"
                        ),
                        "access_groups": [
                            "public",
                            "internal",
                            "finance",
                            "support",
                            "security",
                            "exec",
                        ],
                        "current_only": metadata.status == "current",
                        "generated_by": generator_model,
                        "seed": seed,
                    }
                )

        print(
            f"  {metadata.doc_id:<18} {len([c for c in cases if c['relevant_docs'] == [metadata.doc_id]]):>3} questions"
        )

    # Negative controls carry no relevant_docs, so a ranking metric skips them
    # and the abstention metric picks them up instead.
    for offset, question in enumerate(NEGATIVE_CONTROLS[: args.negatives], start=1):
        cases.append(
            {
                "id": f"NEG-{offset:03d}",
                "category": "unanswerable",
                "query": question,
                "relevant_docs": [],
                "should_abstain": True,
                "lexical_overlap": 0.0,
                "overlap_band": "none",
                "access_groups": ["public", "internal", "finance", "support", "security", "exec"],
                "current_only": True,
                "generated_by": "fixed negative control",
                "seed": seed,
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")

    bands = defaultdict(int)
    for case in cases:
        bands[case["overlap_band"]] += 1
    mean_overlap = sum(c["lexical_overlap"] for c in cases) / len(cases) if cases else 0.0

    print("=" * 84)
    print(f"  Wrote {len(cases)} held-out cases to {args.output.relative_to(ROOT)}")
    print(f"  documents covered : {len({c['relevant_docs'][0] for c in cases})}")
    print(f"  mean lexical overlap with source passage : {mean_overlap:.3f}")
    print(f"    low (<0.35)    {bands['low']:>4}   least leakage, hardest cases")
    print(f"    medium         {bands['medium']:>4}")
    print(f"    high (>=0.6)   {bands['high']:>4}   most leakage, flatters BM25")
    if rejected:
        print(f"  rejected by rule  : {dict(rejected)}")
    print("=" * 84)
    print("\n  This set is HELD OUT. Do not tune retrieval against it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
