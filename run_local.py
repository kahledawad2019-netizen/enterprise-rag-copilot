"""
Run the whole copilot locally: Ollama for chat and embeddings, the UI and API
on one port. Nothing leaves the machine.

    ollama serve                      # if Ollama is not already running
    python run_local.py               # then open http://127.0.0.1:8000

(Use the project's virtualenv - `run_local.ps1` / `run_local.sh` create it on
first run.)

What it does, in order, stopping with a plain explanation if a step fails:

1. Checks Ollama is reachable.
2. Picks the chat model: OLLAMA_CHAT_MODEL if set and installed, otherwise the
   best installed one (instruct models first - see PREFERRED_OLLAMA_CHAT_MODELS).
3. Checks the embedding model is installed (nomic-embed-text by default) and
   pulls it with --pull.
4. Builds the vector index if it is missing or was built with a different
   embedding model; otherwise re-indexes only changed documents.
5. Builds the UI if it has not been built.
6. Serves UI + API at http://127.0.0.1:8000.

Every choice can be overridden with the environment variables in
local-enterprise-copilot/.env.example; values already set in the environment
or in local-enterprise-copilot/.env are respected.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "local-enterprise-copilot"
API = ROOT / "cloud" / "api"
UI = ROOT / "cloud" / "ui"

sys.path.insert(0, str(APP / "src"))
sys.path.insert(0, str(API))

# Local defaults. Applied only where neither the environment nor the project's
# .env file already sets the variable.
LOCAL_DEFAULTS = {
    "LLM_PROVIDER": "ollama",
    "EMBEDDING_PROVIDER": "ollama",
    "OLLAMA_EMBEDDING_MODEL": "nomic-embed-text",
    "COPILOT_PROFILE": "lite",
    # No SQL Server or Postgres is assumed: documents-only until one is set up.
    "DATABASE_BACKEND": "none",
    # A local index of its own, so it never collides with a cloud-built one.
    "QDRANT_MODE": "embedded",
    "QDRANT_PATH": "./data/qdrant-local",
    "QDRANT_INDEX_VERSION": "local-nomic-v1",
    # Single user on localhost: no gateway, no token, no rate limit.
    "API_AUTH_MODE": "public",
    "RATE_LIMIT_PER_MINUTE": "0",
    "COPILOT_DEMO_MODE": "true",
}

# Only these keys are forced for a local run even when .env says otherwise:
# the launcher's whole point is a local, private runtime.
FORCED = {
    "LLM_PROVIDER",
    "EMBEDDING_PROVIDER",
    "API_AUTH_MODE",
    # The local index lives in its own folder so it can never be confused with
    # (or rebuilt over) an index another configuration created.
    "QDRANT_MODE",
    "QDRANT_PATH",
    "QDRANT_INDEX_VERSION",
}


# Honoured from the real environment only, not from .env: the project .env
# predates this launcher and names a GPU-sized profile (8 evidence chunks),
# which roughly doubles CPU prompt time. `lite` (5 chunks) is the CPU default;
# `$env:COPILOT_PROFILE = "standard"` still wins.
ENVIRONMENT_ONLY = {"COPILOT_PROFILE"}


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def fail(message: str, hint: str = "") -> None:
    print(f"\n[FAILED] {message}")
    if hint:
        print(f"         {hint}")
    sys.exit(1)


def apply_defaults() -> None:
    from dotenv import dotenv_values

    file_values = {k.upper(): v for k, v in dotenv_values(APP / ".env").items() if v}
    for key, value in LOCAL_DEFAULTS.items():
        if key in FORCED:
            os.environ[key] = value
        elif key in ENVIRONMENT_ONLY:
            os.environ.setdefault(key, value)
        elif key not in os.environ and key not in file_values:
            os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the copilot locally with Ollama")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--pull", action="store_true", help="pull missing Ollama models")
    parser.add_argument("--rebuild-index", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    apply_defaults()

    from enterprise_copilot.config import get_settings
    from enterprise_copilot.llm.clients import (
        list_ollama_models,
        pick_ollama_chat_model,
    )

    settings = get_settings()

    # 1. Ollama
    step(f"Checking Ollama at {settings.ollama.host}")
    try:
        installed = list_ollama_models(settings.ollama.host)
    except Exception:  # noqa: BLE001 - any failure here means "not reachable"
        fail(
            f"Ollama is not reachable at {settings.ollama.host}.",
            "Start it with `ollama serve` (or open the Ollama app), then run this again.",
        )
    print("    installed: " + (", ".join(m["name"] for m in installed) or "(none)"))

    # 2. Chat model
    configured = os.environ.get("OLLAMA_CHAT_MODEL") or settings.ollama.chat_model
    chat_model = pick_ollama_chat_model(installed, configured)
    if chat_model is None:
        fail(
            "No chat model is installed in Ollama.",
            "Install one, for example: ollama pull qwen3:4b-instruct-2507-q4_K_M",
        )
    if configured and chat_model != configured and f"{configured}:latest" != chat_model:
        print(f"    {configured} is not installed; using {chat_model} instead")
    os.environ["OLLAMA_CHAT_MODEL"] = chat_model
    print(f"    chat model: {chat_model}")

    # 3. Embedding model
    embedding = settings.embedding_model  # honours the environment and .env alike
    names = {m["name"] for m in installed}
    if embedding not in names and f"{embedding}:latest" not in names:
        if not args.pull:
            fail(
                f"Embedding model {embedding} is not installed.",
                f"Run `ollama pull {embedding}` or re-run with --pull.",
            )
        step(f"Pulling {embedding}")
        subprocess.run(["ollama", "pull", embedding], check=True)
    print(f"    embedding model: {embedding}")

    get_settings.cache_clear()
    settings = get_settings()

    # 4. Index
    step("Checking the document index")
    from enterprise_copilot.ingestion.pipeline import IngestionPipeline

    pipeline = IngestionPipeline(settings)
    manifest = pipeline.read_manifest()
    stale = (
        manifest is None
        or manifest.embedding_model != settings.embedding_model
        or manifest.index_version != settings.vector_store.index_version
    )
    report, manifest = pipeline.build_index(rebuild=args.rebuild_index or stale)
    print(f"    {manifest.chunk_count} passages from {manifest.document_count} documents"
          + (" (rebuilt)" if (args.rebuild_index or stale) else " (up to date)"))
    for name, error in report.errors:
        print(f"    [skipped] {name}: {error}")
    # Release the embedded Qdrant lock before the server opens its own client.
    pipeline.store.close()
    del pipeline

    # 5. UI
    if not (UI / "dist" / "index.html").exists():
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if npm is None:
            print("\n    Node.js is not installed, so the UI cannot be built. The API will run at /api.")
        else:
            step("Building the UI (first run only)")
            subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=UI, check=True)
            subprocess.run([npm, "run", "build:open"], cwd=UI, check=True)

    # 6. Serve
    url = f"http://{args.host}:{args.port}"
    step(f"Starting on {url}  (LOCAL — OLLAMA · {chat_model})   Ctrl+C to stop")
    if not args.no_browser:
        import threading

        threading.Timer(3.0, lambda: webbrowser.open(url)).start()

    import uvicorn

    os.chdir(API)
    uvicorn.run("server:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
