r"""
Build or refresh the document index.

    .venv\Scripts\python scripts\build_index.py              # incremental
    .venv\Scripts\python scripts\build_index.py --rebuild    # from scratch
    .venv\Scripts\python scripts\build_index.py --dry-run    # parse only
    .venv\Scripts\python scripts\build_index.py --validate   # check, do not build

Incremental is the default: documents whose content hash is unchanged are
skipped, so editing one file costs one file's worth of embedding.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the document index")
    parser.add_argument("--rebuild", action="store_true", help="drop and rebuild from scratch")
    parser.add_argument("--dry-run", action="store_true", help="parse and chunk without writing")
    parser.add_argument("--validate", action="store_true", help="validate the existing index only")
    parser.add_argument("--documents", type=Path, help="override the document directory")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    from enterprise_copilot.config import get_settings
    from enterprise_copilot.ingestion.pipeline import IngestionPipeline
    from enterprise_copilot.retrieval.embedder import EmbeddingError
    from enterprise_copilot.retrieval.vector_store import VectorStoreError

    settings = get_settings()
    pipeline = IngestionPipeline(settings)

    print("=" * 78)
    print("  Document index")
    print(f"  embedding model : {settings.embedding_model}")
    print(f"  vector store    : qdrant:{settings.vector_store.mode} "
          f"({settings.vector_store.collection})")
    print(f"  index version   : {settings.vector_store.index_version}")
    print(f"  chunk target    : {settings.retrieval.chunk_target_tokens} tokens "
          f"(overlap {settings.retrieval.chunk_overlap_tokens})")
    print("=" * 78)

    try:
        if args.validate:
            ok, problems = pipeline.validate_index()
            if ok:
                manifest = pipeline.read_manifest()
                print(f"[ OK ] Index valid: {manifest.chunk_count} chunks from "
                      f"{manifest.document_count} documents")
                print(f"       built {manifest.built_at_utc} with "
                      f"{manifest.embedding_model} (dim {manifest.embedding_dimension})")
                return 0
            print("[FAIL] Index is not usable:")
            for problem in problems:
                print(f"       - {problem}")
            return 1

        report, manifest = pipeline.build_index(
            root=args.documents, rebuild=args.rebuild, dry_run=args.dry_run
        )

    except EmbeddingError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    except VectorStoreError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    except FileNotFoundError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    finally:
        pipeline.store.close()

    print(f"\nDiscovered        {report.discovered}")
    print(f"Parsed            {report.parsed}")
    print(f"Unchanged/skipped {report.skipped_unchanged}")
    print(f"Failed            {report.failed}")
    print(f"Chunks written    {report.chunks_created}")
    print(f"Documents removed {report.documents_deleted}")
    print(f"Duplicates found  {report.duplicates_detected}")
    if report.embedding_seconds:
        print(f"Embedding time    {report.embedding_seconds:.1f}s "
              f"({report.chunks_created / report.embedding_seconds:.1f} chunks/s)")
    print(f"Total time        {report.total_seconds:.1f}s")

    if report.warnings:
        print(f"\nWarnings ({len(report.warnings)}):")
        for source, message in report.warnings[:15]:
            print(f"  {source}: {message}")

    if report.errors:
        print(f"\nErrors ({len(report.errors)}):")
        for source, message in report.errors:
            print(f"  {source}: {message}")
        return 1

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    print(f"\nIndex now holds {manifest.chunk_count} chunks "
          f"from {manifest.document_count} documents.")
    print("Next: .venv\\Scripts\\python scripts\\search.py \"what is the refund policy?\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
