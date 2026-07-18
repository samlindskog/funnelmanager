"""Request authorization for the leads backend.

The leads backend is internal-only (nginx exposes just its Apollo webhooks),
but every API endpoint still enforces auth: callers (the search backend, the
MCP server) forward the end user's session token, and each request is checked
against the central auth service's ``/api/auth/authorize`` (token validity +
OPA policy for service="leads").

Exempt: the Apollo webhooks (secret-in-path auth — Apollo cannot send bearer
tokens) and the health probe.
"""

import httpx
from fastapi import HTTPException, Request, status

from app.config import Settings, get_settings

SERVICE_NAME = "leads"

_EXEMPT_PATH_PREFIXES = ("/api/leads/webhooks/",)
_EXEMPT_PATHS = ("/api/leads/health",)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


async def enforce_authorization(request: Request) -> None:
    path = request.url.path
    if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PATH_PREFIXES):
        return
    settings: Settings = get_settings()
    token = _bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    url = f"{settings.auth_backend_url.rstrip('/')}/api/auth/authorize"
    body = {
        "token": token,
        "service": SERVICE_NAME,
        "method": request.method,
        "path": path,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service unavailable",
        ) from exc
    if response.status_code == status.HTTP_401_UNAUTHORIZED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service error",
        )
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("allowed"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized",
        )
