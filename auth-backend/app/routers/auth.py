from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app import sessions
from app.config import Settings, get_settings
from app.schemas import Token, UserOut, ValidateRequest
from app.security import (
    authenticate_user,
    get_current_user,
    oauth2_scheme,
    resolve_session,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=Token)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    settings: Settings = Depends(get_settings),
) -> Token:
    if not authenticate_user(form_data.username, form_data.password, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = await sessions.create_session(form_data.username, settings)
    return Token(access_token=token)


@router.get("/me", response_model=UserOut)
async def me(current_user: UserOut = Depends(get_current_user)) -> UserOut:
    return current_user


@router.post("/validate", response_model=UserOut)
async def validate(
    body: ValidateRequest,
    settings: Settings = Depends(get_settings),
) -> UserOut:
    """Introspect a session token for other backends.

    Internal service-to-service call — intentionally has no auth guard of its own
    (the token in the body *is* the credential being checked).
    """
    return await resolve_session(body.token, settings)


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
