#!/usr/bin/env sh
# Run the copilot locally with Ollama (macOS / Linux / Git Bash).
#
#   ./run_local.sh             # first run creates the virtualenv and installs
#   ./run_local.sh --pull      # also pull a missing embedding model
#
# Arguments are passed to run_local.py (see its --help).
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/local-enterprise-copilot"

if [ -x "$APP/.venv/bin/python" ]; then
  PY="$APP/.venv/bin/python"
elif [ -x "$APP/.venv/Scripts/python.exe" ]; then
  PY="$APP/.venv/Scripts/python.exe"
else
  echo "==> Creating the virtualenv (first run only)"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 "$APP/.venv"
  else
    python3 -m venv "$APP/.venv"
  fi
  if [ -x "$APP/.venv/bin/python" ]; then PY="$APP/.venv/bin/python"; else PY="$APP/.venv/Scripts/python.exe"; fi
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" -e "$APP[dev]" "uvicorn[standard]"
  else
    "$PY" -m pip install -e "$APP[dev]" "uvicorn[standard]"
  fi
fi

exec "$PY" "$ROOT/run_local.py" "$@"
