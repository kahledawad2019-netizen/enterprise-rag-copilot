"""Document Explorer - browse the corpus, its versions and its conflicts."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
for _candidate in (ROOT / "src", ROOT / "app"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

st.set_page_config(page_title="Document Explorer", page_icon="::", layout="wide")

from _shared import sidebar  # noqa: E402

from enterprise_copilot.config import get_settings  # noqa: E402
from enterprise_copilot.ingestion.chunking import ChunkingConfig, StructureAwareChunker  # noqa: E402
from enterprise_copilot.ingestion.parsers import ParserRegistry  # noqa: E402


@st.cache_data(show_spinner="Parsing documents...")
def load_documents() -> list[dict]:
    """Parsed corpus metadata. Safe to cache: it is public, deterministic
    and identical for every user."""
    settings = get_settings()
    registry = ParserRegistry()
    chunker = StructureAwareChunker(ChunkingConfig.from_settings(settings))

    rows = []
    for path in sorted(settings.documents_dir.glob("*.md")):
        document = registry.parse(path)
        metadata = document.metadata
        rows.append({
            "doc_id": metadata.doc_id,
            "title": metadata.title,
            "type": str(metadata.doc_type),
            "version": metadata.version,
            "status": str(metadata.status),
            "authority": str(metadata.authority),
            "effective": metadata.effective_date.isoformat(),
            "access_group": metadata.access_group,
            "tenant": metadata.tenant,
            "sections": len(document.sections),
            "chunks": len(chunker.chunk_document(document)),
            "words": len(document.text.split()),
            "supersedes": metadata.supersedes or "",
            "superseded_by": metadata.superseded_by or "",
            "_text": document.text,
            "_path": str(path.name),
        })
    return rows


def main() -> None:
    sidebar("docs")
    st.title("Document Explorer")
    st.caption("The corpus the copilot reads, including the deliberate conflicts.")

    documents = load_documents()
    import pandas as pd

    frame = pd.DataFrame(documents).drop(columns=["_text"])

    columns = st.columns(4)
    columns[0].metric("Documents", len(documents))
    columns[1].metric("Chunks", int(frame["chunks"].sum()))
    columns[2].metric("Words", f'{int(frame["words"].sum()):,}')
    columns[3].metric("Superseded", int((frame["status"] == "superseded").sum()))

    st.subheader("Deliberate conflicts")
    st.markdown("""
These exist so version filtering and authority ranking can be *tested* rather
than asserted:

| Conflict | Documents | Type |
|---|---|---|
| Annual refunds: "non-refundable" vs "pro-rata within 30 days" | DOC-REF-000 v1.0 -> DOC-REF-001 v2.1 | version |
| Enterprise P1 first response: 30 min vs 15 min | DOC-SLA-000 v1.0 -> DOC-SLA-001 v2.0 | version |
| Max discount: 25% (policy) vs 30% (guidance) | DOC-PRC-001 vs DOC-SAL-001 | authority |

The SLA conflict mirrors `support.sla_policies` in the database exactly, so the
documents and the structured data share one history.
    """)

    st.subheader("Corpus")
    show_superseded = st.checkbox("Include superseded versions", value=True)
    view = frame if show_superseded else frame[frame["status"] == "current"]
    st.dataframe(view, use_container_width=True, hide_index=True)

    st.subheader("Read a document")
    choice = st.selectbox("Document", [d["doc_id"] + " - " + d["title"] for d in documents])
    selected = next(d for d in documents if choice.startswith(d["doc_id"]))

    meta_columns = st.columns(4)
    meta_columns[0].metric("Version", selected["version"])
    meta_columns[1].metric("Status", selected["status"])
    meta_columns[2].metric("Authority", selected["authority"])
    meta_columns[3].metric("Access", selected["access_group"])

    if selected["status"] == "superseded":
        st.warning(
            f"This version is no longer in force. Superseded by "
            f"{selected['superseded_by']}. Retrieval excludes it by default."
        )
    if selected["doc_id"] == "DOC-TST-001":
        st.error(
            "This is the prompt-injection TEST document. It is excluded from normal "
            "retrieval. Its payloads are quoted data, never instructions."
        )

    st.markdown(f"`{selected['_path']}`")
    st.text(selected["_text"])


main()
