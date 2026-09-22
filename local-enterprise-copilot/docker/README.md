# Docker assets (optional)

Docker is **not required** by this project and was **not installed** on the
reference machine. Qdrant therefore runs in embedded (local path) mode by
default -- see `docs/architecture_decisions/ADR-001-runtime-platform.md`.

These files exist so that a team with Docker available can run Qdrant and
Phoenix as services instead, by setting in `.env`:

    QDRANT_MODE=server
    QDRANT_URL=http://localhost:6333
    OBS_ENABLE_PHOENIX=true

No application code changes are needed for that switch.
