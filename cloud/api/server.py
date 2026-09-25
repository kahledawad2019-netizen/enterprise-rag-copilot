"""
Single-origin server: the React UI at `/` and the API at `/api`.

`main:app` is the API on its own, which is what the Cloudflare Worker gateway
proxies to. This module is for the two deployments that have no gateway - the
local app and the single-container cloud demo - where serving both from one
process means one URL, no CORS, and no build-time API address to get wrong.

    uvicorn server:app --port 8000

Starlette does not run the lifespan of a mounted sub-application, so the
API's lifespan (which builds the copilot) is run explicitly here. Without that
every request would answer 503 "the copilot did not start".
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from main import app as api

UI_DIST = Path(os.environ.get("UI_DIST_DIR", Path(__file__).resolve().parent.parent / "ui" / "dist"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with api.router.lifespan_context(api):
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/api", api)


@app.get("/healthz", include_in_schema=False)
def liveness() -> dict[str, str]:
    """Liveness only: the process is up. Readiness is /api/health."""
    return {"status": "alive"}


if UI_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        # Real files (favicon, robots.txt) are served as themselves; every
        # other path is a client-side route and gets the app shell. The
        # resolved path must stay inside dist - no traversal via "..".
        candidate = (UI_DIST / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(UI_DIST.resolve()):
            return FileResponse(candidate)
        return FileResponse(UI_DIST / "index.html", headers={"Cache-Control": "no-cache"})

else:

    @app.get("/", include_in_schema=False)
    def no_ui() -> JSONResponse:
        return JSONResponse(
            {"detail": "UI not built. Run `npm run build` in cloud/ui. API is at /api."},
            status_code=503,
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
