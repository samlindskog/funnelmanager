"""Request auth for the search backend.

This backend is a public API but issues no tokens. Every request's bearer
token is sent to the central auth service's ``/api/auth/authorize`` endpoint,
which validates the session AND evaluates the OPA policy for
(service="search", method, path). 401 -> invalid/expired token,
403 -> valid token but the caller's role has no grant covering this call.
"""

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from app.config import Settings, get_settings
from app.schemas import UserOut

SERVICE_NAME = "search"

# Login/refresh live on the auth service (nginx routes /api/auth/* there). This
# scheme only extracts the bearer token from the request for authorization.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> UserOut:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    url = f"{settings.auth_backend_url.rstrip('/')}/api/auth/authorize"
    body = {
        "token": token,
        "service": SERVICE_NAME,
        "method": request.method,
        "path": request.url.path,
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
        raise credentials_exception
    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service error",
        )
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("username"):
        raise credentials_exception
    if not payload.get("allowed"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized",
        )
    return UserOut(username=str(payload["username"]), role=str(payload.get("role") or ""))
