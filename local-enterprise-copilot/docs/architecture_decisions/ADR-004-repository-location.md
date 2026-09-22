# ADR-004: Project lives in a subdirectory, preserving the existing prototype

- **Status:** Accepted
- **Date:** 2026-09-18

## Context

The workspace `D:\khaled\RAG SYSTEM` already contained a working Vanna +
ChromaDB + Ollama prototype built against the `BikeStores` sample database:
`app.py`, `ask.py`, `src/`, `sql/`, `README.md`, `requirements.txt`, a `.venv`,
and a trained Chroma store. It was staged in git but **not yet committed**, so
there was no history to fall back on.

The specification's suggested layout is rooted at `local-enterprise-copilot/`
and defines its own `src/`, `sql/`, `README.md` and `requirements`. Building at
the workspace root would have overwritten `README.md`, collided with `src/` and
`sql/`, and destroyed uncommitted work.

The engineering rules are explicit: *"Preserve user files and existing work. Do
not delete or overwrite unrelated content."* They also permit adapting the
suggested structure "when there is a concrete technical reason", provided the
reason is documented. This is that document.

## Decision

Build the project at `D:\khaled\RAG SYSTEM\local-enterprise-copilot\`, using
the specification's layout verbatim inside it.

The prototype is untouched. It remains useful: it is where the Vanna 2.x API
was first exercised against a live SQL Server, and where the `extract_sql`
bracket defect recorded in ADR-002 was found.

## Consequences

**Positive**
- No uncommitted work was destroyed.
- The internal structure matches the specification exactly, so every path in
  the spec resolves.
- The prototype stays available as a reference and a smaller worked example.

**Negative**
- One extra directory level. `cd local-enterprise-copilot` is required before
  any command in the documentation.
- Two virtual environments exist on disk (the prototype's and this project's).
  The prototype's can be deleted once it is no longer wanted; nothing in this
  project depends on it.

## If the root is preferred instead

Moving the project up one level is mechanical, and should be done only after
the prototype is committed or deliberately discarded:

```powershell
cd 'D:\khaled\RAG SYSTEM'
git add -A; git commit -m "Vanna/BikeStores prototype"   # preserve it in history
# then move local-enterprise-copilot/* to the root and delete the old files
```

The project uses only relative paths anchored on `PROJECT_ROOT`, which is
derived from `settings.py`'s own location, so no configuration needs editing
after such a move.
