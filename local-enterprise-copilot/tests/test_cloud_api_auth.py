from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

API_PATH = Path(__file__).resolve().parents[2] / "cloud" / "api" / "main.py"
SPEC = importlib.util.spec_from_file_location("copilot_cloud_api_for_tests", API_PATH)
assert SPEC is not None and SPEC.loader is not None
api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api
SPEC.loader.exec_module(api)


def test_production_identity_is_mapped_server_side(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_TOKEN", "service-secret")
    monkeypatch.setenv("COPILOT_IDENTITY_MAP_JSON", '{"alice@example.com":"analyst_na"}')
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(demo_mode=False))

    caller = api.require_caller("Bearer service-secret", "Alice@Example.com")
    key, _ = api._resolve_persona(caller, "admin")

    assert caller.email == "alice@example.com"
    assert key == "analyst_na"


def test_unmapped_production_identity_is_forbidden(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_TOKEN", "service-secret")
    monkeypatch.setenv("COPILOT_IDENTITY_MAP_JSON", "{}")
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(demo_mode=False))

    with pytest.raises(HTTPException) as exc_info:
        api.require_caller("Bearer service-secret", "unknown@example.com")

    assert exc_info.value.status_code == 403


def test_demo_mode_is_the_only_mode_that_can_switch_persona(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_TOKEN", "service-secret")
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(demo_mode=True))

    caller = api.require_caller("Bearer service-secret", "")
    key, persona = api._resolve_persona(caller, "admin")

    assert key == "admin"
    assert persona["is_admin"] is True


def test_evaluation_marks_changed_embedding_as_historical(monkeypatch) -> None:
    settings = SimpleNamespace(
        embeddings=SimpleNamespace(provider="cloudflare"),
        embedding_model="@cf/baai/bge-m3",
        vector_store=SimpleNamespace(index_version="bge-m3-v1"),
    )
    monkeypatch.setattr(api, "get_settings", lambda: settings)

    report = api.evaluation(None)["report"]

    assert report["current"] is False
    assert report["evaluated_embedding_model"] == "qwen3-embedding:0.6b"
    assert report["live_embedding_model"] == "@cf/baai/bge-m3"
    assert len(report["stale_reasons"]) == 3
