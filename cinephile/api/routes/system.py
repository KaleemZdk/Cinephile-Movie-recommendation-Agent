"""Service health."""

from __future__ import annotations

from fastapi import APIRouter

from cinephile import __version__
from cinephile.api.deps import RuntimeDep

router = APIRouter(tags=["system"])


@router.get("/health")
def health(runtime: RuntimeDep) -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "agent_ready": runtime.ready,
        "models": runtime.model_status(),
    }
