"""Offline regression coverage for the cloud transport, uploads and SPA."""

from __future__ import annotations

import builtins
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from enterprise_copilot.config import get_settings
from enterprise_copilot.ingestion.parsers import MarkdownParser
from enterprise_copilot.models.evidence import Answer, Evidence, EvidencePackage
from enterprise_copilot.routing.orchestrator import CopilotTrace, Prepared

API_DIR = Path(__file__).resolve().parents[2] / "cloud" / "api"
QUESTION = "What is the policy?"


def load_module(monkeypatch, name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class StubCopilot:
    def __init__(self):
        self.calls = []
        self.retriever = SimpleNamespace(store=SimpleNamespace(count=lambda: 1))
        self.trace = CopilotTrace(trace_id="test-trace", question=QUESTION)
        self.package = EvidencePackage(
            question=QUESTION,
            document_evidence=[Evidence(evidence_id="D1", evidence_type="document", text="Policy")],
        )
        self.answer = Answer(question=QUESTION, text="Hello world", evidence=self.package)

    def ask(self, question, **kwargs):
        self.calls.append((question, kwargs))
        return self.answer, self.trace

    def ask_stream(self, question, **kwargs):
        self.calls.append((question, kwargs))
        yield "prepared", Prepared(trace=self.trace, package=self.package)
        yield "token", "Hello "
        yield "token", "world"
        yield "answer", self.answer


@pytest.fixture
def api(monkeypatch, tmp_path):
    get_settings.cache_clear()
    monkeypatch.setenv("API_AUTH_MODE", "public")
    monkeypatch.setenv("DATABASE_BACKEND", "none")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
    monkeypatch.setenv("MAX_UPLOADED_DOCUMENTS", "25")
    monkeypatch.setenv("MAX_UPLOAD_MB", "5")
    # main.py changes sys.path and imports its sibling by bare name.
    monkeypatch.setattr(sys, "path", list(sys.path))
    load_module(monkeypatch, "uploads", API_DIR / "uploads.py")
    module = load_module(monkeypatch, "copilot_cloud_features_tests", API_DIR / "main.py")
    module._rate_hits.clear()
    monkeypatch.setattr(module, "Copilot", Mock(side_effect=AssertionError("Real Copilot started")))
    monkeypatch.setattr(module, "_copilot", StubCopilot())
    settings = get_settings()
    monkeypatch.setattr(settings, "documents_dir", tmp_path)
    try:
        yield module
    finally:
        module._rate_hits.clear()
        get_settings.cache_clear()


@pytest.fixture
def client(api):
    # Deliberately do not enter TestClient: entering starts the real lifespan.
    transport = TestClient(api.app)
    try:
        yield transport
    finally:
        transport.close()


def test_public_meta_and_selectable_personas(api, client):
    response = client.get("/meta")
    assert response.status_code == 200
    assert {p["key"] for p in response.json()["personas"]} == set(api.PERSONAS)
    for persona in ("guest", "admin", "analyst_eu"):
        response = client.post("/ask", json={"question": QUESTION, "persona": persona})
        assert response.status_code == 200
        assert response.json()["answer"] == "Hello world"
        question, call = api._copilot.calls[-1]
        assert question == QUESTION
        expected = api.PERSONAS[persona]
        assert call["user"].access_groups == expected["access_groups"]
        assert call["user"].tenant == expected["tenant"]
        assert call["user"].is_admin == expected["is_admin"]
        assert call["tenant_id"] == expected["tenant_id"]


@pytest.mark.parametrize("path", ["/meta", "/ask"])
def test_default_token_mode_requires_bearer(api, client, monkeypatch, path):
    monkeypatch.delenv("API_AUTH_MODE")
    monkeypatch.setenv("BACKEND_TOKEN", "test-secret")
    response = client.get(path) if path == "/meta" else client.post(path, json={"question": QUESTION})
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert api._copilot.calls == []


def test_rate_limit_is_per_forwarded_address(api, client, monkeypatch):
    # One trusted proxy: the bucket is the rightmost X-Forwarded-For entry,
    # the one the proxy appended. The leftmost is whatever the caller sent.
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setattr(api.time, "monotonic", lambda: 100.0)
    for expected, spoofed in ((200, "1.1.1.1"), (200, "2.2.2.2"), (429, "3.3.3.3")):
        response = client.post(
            "/ask", json={"question": QUESTION},
            headers={"X-Forwarded-For": f"{spoofed}, 192.0.2.1"},
        )
        assert response.status_code == expected
    assert response.json() == {
        "error": "rate_limited",
        "detail": "Too many requests. Please wait a minute and try again.",
    }
    assert client.post(
        "/ask", json={"question": QUESTION}, headers={"X-Forwarded-For": "192.0.2.2"},
    ).status_code == 200
    assert len(api._copilot.calls) == 3


def test_documents_only_health_never_imports_or_opens_database(api, client, monkeypatch):
    import enterprise_copilot.database.connection as connection
    import enterprise_copilot.llm as llm

    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_BACKEND", "none")
    get_settings.cache_clear()
    settings = get_settings()
    connect = Mock(side_effect=AssertionError("Database must remain disabled"))
    monkeypatch.setattr(connection, "raw_connection", connect)
    monkeypatch.setattr(llm, "build_chat_client", lambda _: SimpleNamespace(
        list=lambda: {"models": [{"model": settings.chat_model}]},
    ))
    original_import = builtins.__import__
    db_imports = []

    def checked_import(name, *args, **kwargs):
        if name == "enterprise_copilot.database.connection":
            db_imports.append(name)
            raise AssertionError("Health must not import a database connection")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    response = client.get("/health")
    assert response.status_code == 200
    database = next(c for c in response.json()["checks"] if c["name"] == "database")
    assert database == {"name": "database", "ok": True, "detail": "disabled (documents only)"}
    connect.assert_not_called()
    assert db_imports == []


def test_public_errors_are_generic_and_deduplicated(api):
    errors = [
        r"retrieval: VectorStoreError: C:\secret\path",
        "generation: https://api.groq.com returned 401 with gsk_abc",
        "retrieval: another secret", "generation: another key",
        "sql_execution: password=secret", "unknown: secret", "other: secret",
    ]
    assert api._public_errors(errors) == [
        "Document search was unavailable for this question.",
        "The language model was unavailable.",
        "The database step could not complete.",
        "A pipeline step failed.",
    ]


def sse_events(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return [
        (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        for block in response.text.strip().split("\n\n")
        if (lines := block.splitlines()) and not lines[0].startswith(":")
    ]


def test_stream_sends_sources_tokens_then_answer(client):
    events = sse_events(client.post("/ask/stream", json={"question": QUESTION}))
    assert [kind for kind, _ in events] == ["sources", "token", "token", "done"]
    assert events[0][1]["trace_id"] == "test-trace"
    assert events[0][1]["sources"][0]["id"] == "D1"
    assert [payload for kind, payload in events if kind == "token"] == [
        {"text": "Hello "}, {"text": "world"},
    ]
    assert events[-1][1]["answer"] == "Hello world"
    assert events[-1][1]["trace_id"] == "test-trace"


def test_stream_exception_is_generic(api, client, monkeypatch):
    def broken_stream(*args, **kwargs):
        raise RuntimeError(r"C:\secret\path https://api.groq.com gsk_abc")
        yield  # pragma: no cover -- retain generator semantics

    monkeypatch.setattr(api._copilot, "ask_stream", broken_stream)
    assert sse_events(client.post("/ask/stream", json={"question": QUESTION})) == [
        ("error", {"error": "internal", "detail": "The request failed."}),
    ]


@pytest.mark.parametrize("filename,content,limit,message", [
    ("bad.exe", b"data", 100, "Unsupported file type .exe. Upload .md, .txt, .pdf or .docx."),
    ("empty.txt", b"", 100, "The file is empty."),
    ("large.txt", b"x" * (1024 * 1024 + 1), 1024 * 1024, "The file is larger than 1 MB."),
    ("fake.pdf", b"not PDF", 100, "The file does not look like a PDF."),
    ("fake.docx", b"not ZIP", 100, "The file does not look like a .docx document."),
    ("invalid.txt", b"\xff", 100, "Text files must be UTF-8."),
], ids=["extension", "empty", "oversize", "pdf-magic", "docx-magic", "utf8"])
def test_upload_validation_and_storage_reject_invalid_files(api, tmp_path, filename, content, limit, message):
    for operation in (
        lambda: api.uploads.validate(filename, content, max_bytes=limit),
        lambda: api.uploads.store(filename, content, tmp_path, max_bytes=limit),
    ):
        with pytest.raises(api.uploads.UploadRejectedError) as exc:
            operation()
        assert str(exc.value) == message
    assert list(tmp_path.iterdir()) == []


def test_forwarded_header_is_ignored_without_a_trusted_proxy(api, client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setattr(api.time, "monotonic", lambda: 100.0)
    first = client.post("/ask", json={"question": QUESTION}, headers={"X-Forwarded-For": "1.1.1.1"})
    second = client.post("/ask", json={"question": QUESTION}, headers={"X-Forwarded-For": "2.2.2.2"})
    assert (first.status_code, second.status_code) == (200, 429)


def test_upload_without_content_length_or_too_large_is_refused_before_parsing(client, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    too_large = client.post(
        "/documents/upload",
        content=b"x",
        headers={"Content-Length": str(5 * 1024 * 1024), "Content-Type": "multipart/form-data; boundary=b"},
    )
    assert too_large.status_code == 413


def test_upload_ids_do_not_collide_on_similar_short_files(api):
    ids = {api.uploads.doc_id_for(f"# Upload\n\nPolicy number {n}.\n".encode()) for n in range(2000)}
    assert len(ids) == 2000


@pytest.mark.parametrize("filename,content", [
    ("notes.txt", b"Useful body"),
    ("../../evil name.md", b"---\naccess_group: exec\ndoc_id: DOC-FAKE-0001\n---\nUseful body"),
])
def test_stored_markdown_has_safe_metadata_and_parses(api, tmp_path, filename, content):
    path, doc_id = api.uploads.store(filename, content, tmp_path, max_bytes=1024)
    assert path.suffix == ".md"
    assert path.resolve().parent == (tmp_path / "uploads").resolve()
    assert re.fullmatch(r"DOC-UPL-\d{4}-\d{4}", doc_id)
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    assert "access_group: exec" not in raw
    assert "DOC-FAKE-0001" not in raw
    parsed = MarkdownParser().parse(path)
    assert parsed.metadata.doc_id == doc_id
    assert parsed.metadata.access_group == "public"
    assert parsed.metadata.tenant == "all"
    assert parsed.metadata.doc_type == "uploaded"
    assert "Useful body" in parsed.text


@pytest.mark.parametrize("enabled,status,detail", [
    ("false", 403, "Uploads are disabled on this deployment."),
    ("true", 400, "The file does not look like a PDF."),
])
def test_upload_endpoint_rejects_before_indexing(api, client, monkeypatch, tmp_path, enabled, status, detail):
    monkeypatch.setenv("UPLOADS_ENABLED", enabled)
    pipeline = Mock(side_effect=AssertionError("Indexing must not start"))
    monkeypatch.setitem(sys.modules, "enterprise_copilot.ingestion.pipeline", SimpleNamespace(
        IngestionPipeline=pipeline,
    ))
    response = client.post("/documents/upload", files={
        "file": ("secret.pdf", b"invalid PDF gsk_abc", "application/pdf"),
    })
    assert response.status_code == status
    assert response.json() == {
        "error": "forbidden" if status == 403 else "bad_request", "detail": detail,
    }
    pipeline.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_spa_fallback_and_encoded_traversal(api, monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "assets").mkdir()
    (dist / "index.html").write_text("<html>Test shell</html>", encoding="utf-8")
    (tmp_path / "private.txt").write_text("outside-secret", encoding="utf-8")
    monkeypatch.setenv("UI_DIST_DIR", str(dist))
    monkeypatch.setenv("API_AUTH_MODE", "public")
    # server imports main by name; bind the isolated, already stubbed module.
    monkeypatch.setitem(sys.modules, "main", api)
    server = load_module(monkeypatch, "copilot_cloud_server_features_tests", API_DIR / "server.py")
    transport = TestClient(server.app)
    try:
        for path in ("/some/client/route", "/%2e%2e/private.txt", "/%2e%2e/%2e%2e/etc/passwd"):
            response = transport.get(path)
            assert response.status_code == 200
            assert response.text == "<html>Test shell</html>"
    finally:
        transport.close()
