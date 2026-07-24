"""Authenticated identity probe, identical on every service.

GET /api/{service}/whoami returns a summary of the acting principal. Unlike
the observability routes it is deliberately NOT @anonymous: answering requires
a valid per-hop JWT plus a covering role grant (OPA in the mesh,
FM_ENFORCE_GRANTS in compose). That makes it double as the hub's app-discovery
probe — 2xx means "this user can use this service", 401/403 hides the tile.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from fm_runtime.context import current_principal


def install_whoami(app: Any, service: str) -> None:
    """Add GET /api/{service}/whoami to a FastAPI app."""

    @app.get(f"/api/{service}/whoami", include_in_schema=False)
    async def whoami() -> dict[str, Any]:
        principal = current_principal()
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return {
            "service": service,
            "sub": principal.sub,
            "username": principal.username,
            "roles": list(principal.roles),
            "client_id": principal.client_id,
            "delegated": principal.is_delegated,
        }
