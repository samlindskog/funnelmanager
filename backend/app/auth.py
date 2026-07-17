"""Request auth for the search backend.

This backend is a public API but issues no tokens. It extracts the caller's
bearer token and validates it against the central auth service, which owns the
session store. Internal callers (e.g. the leads backend) need no auth.
"""

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.config import Settings, get_settings
from app.schemas import UserOut

# Login/refresh live on the auth service (nginx routes /api/auth/* there). This
# scheme only extracts the bearer token from the request for validation.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> UserOut:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    url = f"{settings.auth_backend_url.rstrip('/')}/api/auth/validate"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json={"token": token})
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
    username = payload.get("username") if isinstance(payload, dict) else None
    if not username:
        raise credentials_exception
    return UserOut(username=str(username))
