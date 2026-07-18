"""Public + any-profile auth routes.

Access tiers (enforced here; management routes live in ``admin.py``):

- Anonymous: ``login``, ``request-account``, ``health``.
- Any valid profile: ``me``, ``logout``, ``apps``, ``validate`` and
  ``authorize`` (the OPA decision endpoint — the token in the body *is* the
  credential being checked, so every profile may call them).
- Everything else on this service is admin-gated in ``admin.py``.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app import opa, sessions, store
from app.config import DEFAULT_WEB_APPS, Settings, get_settings
from app.schemas import (
    AccountRequestIn,
    AppLink,
    AuthorizeRequest,
    AuthorizeResponse,
    Token,
    UserOut,
    ValidateRequest,
)
from app.security import (
    authenticate_user,
    get_current_user,
    oauth2_scheme,
    resolve_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=Token)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    settings: Settings = Depends(get_settings),
) -> Token:
    user = await authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = await sessions.create_session(str(user["username"]), settings)
    return Token(access_token=token)


@router.post("/request-account", status_code=status.HTTP_202_ACCEPTED)
async def request_account(body: AccountRequestIn) -> dict[str, str]:
    """Anonymous account request from the landing page (username only).

    Always answers 202 with a generic message — it never reveals whether a
    username exists, is pending, or was invalid beyond basic format checks.
    """
    username = store.normalize_username(body.username)
    if not store.valid_username(username):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Username must be 3-32 chars: lowercase letters, digits, . _ -",
        )
    if not await store.get_user(username):
        await store.upsert_account_request(username)
    return {"status": "received"}


@router.get("/me", response_model=UserOut)
async def me(current_user: UserOut = Depends(get_current_user)) -> UserOut:
    return current_user


@router.get("/apps", response_model=list[AppLink])
async def apps(
    _: UserOut = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> list[AppLink]:
    """Web apps shown on the hub page after login (configured via WEB_APPS)."""
    try:
        raw = json.loads(settings.web_apps.strip() or DEFAULT_WEB_APPS)
    except ValueError:
        logger.warning("WEB_APPS is not valid JSON; returning no apps")
        return []
    out: list[AppLink] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name") and item.get("url"):
                out.append(
                    AppLink(
                        name=str(item["name"]),
                        description=str(item.get("description") or ""),
                        url=str(item["url"]),
                    )
                )
    return out


@router.post("/validate", response_model=UserOut)
async def validate(
    body: ValidateRequest,
    settings: Settings = Depends(get_settings),
) -> UserOut:
    """Introspect a session token for other backends.

    Intentionally has no auth guard of its own — the token in the body *is*
    the credential being checked.
    """
    return await resolve_session(body.token, settings)


@router.post("/authorize", response_model=AuthorizeResponse)
async def authorize(
    body: AuthorizeRequest,
    settings: Settings = Depends(get_settings),
) -> AuthorizeResponse:
    """Validate a session token AND evaluate the OPA policy for one request.

    This is the one call every backend makes per incoming request:
    401 -> token invalid/expired; 200 with allowed=false -> deny (403 at the
    caller); 503 -> OPA unavailable (fail closed).
    """
    user = await resolve_session(body.token, settings)
    try:
        allowed = await opa.authorize(
            username=user.username,
            role=user.role,
            service=body.service,
            method=body.method,
            path=body.path,
            settings=settings,
        )
    except opa.OpaUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authorization service unavailable",
        ) from exc
    return AuthorizeResponse(allowed=allowed, username=user.username, role=user.role)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> Response:
    await sessions.delete_session(token, settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
