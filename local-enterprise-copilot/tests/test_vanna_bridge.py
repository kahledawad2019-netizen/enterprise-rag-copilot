"""Offline regression coverage for Vanna's provider-agnostic chat bridge."""

from unittest.mock import Mock, call

import pytest

from enterprise_copilot.config import get_settings
from enterprise_copilot.text_to_sql.vanna_provider import _build_vanna_class

pytest.importorskip("vanna")
pytest.importorskip("chromadb")


@pytest.fixture
def bridge_settings(tmp_path):
    settings = get_settings().model_copy(deep=True)
    settings.project_root = tmp_path
    settings.database_backend = "duckdb"
    return settings


@pytest.fixture(autouse=True)
def no_ollama(monkeypatch):
    client = Mock(side_effect=AssertionError("Unexpected Ollama client construction"))
    pull = Mock(side_effect=AssertionError("Unexpected Ollama model pull"))
    monkeypatch.setattr("ollama.Client", client)
    monkeypatch.setattr("ollama.pull", pull)
    yield
    client.assert_not_called()
    pull.assert_not_called()


@pytest.fixture
def chroma_init(monkeypatch):
    # The bridge owns directory creation; no database or embedding model is needed.
    init = Mock(return_value=None)
    monkeypatch.setattr("vanna.legacy.chromadb.ChromaDB_VectorStore.__init__", init)
    return init


@pytest.mark.parametrize("role", ["system", "user", "assistant"])
def test_message_helpers(bridge_settings, chroma_init, role):
    bridge = _build_vanna_class()(bridge_settings)

    assert getattr(bridge, f"{role}_message")("Keep this content") == {
        "role": role,
        "content": "Keep this content",
    }


@pytest.mark.parametrize("provider", ["ollama", "groq"])
def test_submit_prompt_caches_configured_chat_client(
    bridge_settings, chroma_init, monkeypatch, provider
):
    bridge_settings.llm.provider = provider
    bridge_settings.llm.model = "llama-3.3-70b-versatile" if provider == "groq" else "llama3.1:8b"
    client = Mock()
    client.chat.side_effect = [
        {"message": {"content": "SELECT 1;"}},
        {"message": {"content": "SELECT 2;"}},
    ]
    build_client = Mock(return_value=client)
    monkeypatch.setattr("enterprise_copilot.llm.build_chat_client", build_client)
    bridge = _build_vanna_class()(bridge_settings)
    build_client.assert_not_called()
    prompts = [
        [bridge.system_message("Return SQL"), bridge.user_message("First query")],
        [bridge.assistant_message("SELECT 1;"), bridge.user_message("Second query")],
    ]

    assert bridge.submit_prompt(prompts[0]) == "SELECT 1;"
    assert bridge.submit_prompt(prompts[1]) == "SELECT 2;"

    build_client.assert_called_once_with(bridge_settings)
    assert build_client.call_args.args[0] is bridge_settings
    assert client.chat.call_args_list == [
        call(
            model=bridge_settings.chat_model,
            messages=prompt,
            options={
                "num_ctx": bridge_settings.profile.chat_context_tokens,
                "temperature": 0.0,
                "num_predict": 800,
            },
            keep_alive=bridge_settings.ollama.keep_alive,
        )
        for prompt in prompts
    ]


@pytest.mark.parametrize(
    ("backend", "directory"),
    [("duckdb", "vanna_chroma_duckdb"), ("sqlserver", "vanna_chroma")],
)
def test_store_directory_is_backend_specific(bridge_settings, chroma_init, backend, directory):
    bridge_settings.database_backend = backend
    store_path = bridge_settings.project_root / "data" / directory
    assert not store_path.exists()

    bridge = _build_vanna_class()(bridge_settings)

    assert store_path.is_dir()
    assert set(store_path.parent.iterdir()) == {store_path}
    chroma_init.assert_called_once_with(
        bridge, config={"path": str(store_path), "client": "persistent", "n_results": 6}
    )
