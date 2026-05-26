"""FastAPI entry point for the governance-tagger app.

Loads the API router, starts the async audit writer at startup, mounts the
built React SPA from ``frontend/dist`` for production serving.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server import audit_logger
from server.routes.api import router as api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    audit_logger.start()
    yield
    audit_logger.stop()


app = FastAPI(title="Governance Tagger", lifespan=lifespan)

app.include_router(api_router, prefix="/api")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# Serve the built React frontend.
_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend", "dist")
_ASSETS_DIR = os.path.join(_FRONTEND_DIR, "assets")

if os.path.isdir(_ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")


@app.get("/{full_path:path}")
def spa(full_path: str):
    # API routes are mounted on /api; everything else is the SPA fallback.
    if full_path.startswith("api/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    index = os.path.join(_FRONTEND_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return JSONResponse(
        {
            "error": "Frontend not built. Run `npm run build` in the frontend/ dir.",
        },
        status_code=500,
    )
