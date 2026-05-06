"""Backend API for the Hermes Studio dashboard plugin."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from hermes_constants import get_hermes_home

router = APIRouter()


def _studio_root() -> Path:
    """Return the canonical Studio runtime root.

    Production keeps live Studio data in /opt/data/studio. Tests and alternate
    profiles fall back to HERMES_HOME/studio through get_hermes_home().
    """
    production_root = Path("/opt/data/studio")
    if production_root.exists() or get_hermes_home() == Path("/opt/data"):
        return production_root
    return get_hermes_home() / "studio"


@router.get("/health")
async def studio_plugin_health() -> dict:
    """Small smoke endpoint used by the plugin shell and tests."""
    root = _studio_root()
    return {
        "ok": True,
        "message": "Studio plugin API connected",
        "runtime_root": str(root),
        "spaces_root": str(root / "spaces"),
    }
