"""Password checks and the session-token dependency for the auth service."""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext

from app import sessions
from app.config import Settings, get_settings
from app.schemas import UserOut

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def authenticate_user(username: str, password: str, settings: Settings) -> bool:
    if username != settings.auth_username:
        return False
    # Support a bcrypt hash in AUTH_PASSWORD, else compare the plain value.
    stored = settings.auth_password
    if stored.startswith("$2"):
        return verify_password(password, stored)
    return password == stored


async def resolve_session(token: str, settings: Settings) -> UserOut:
    """Validate a raw session token against the store; raise 401 if unknown/expired."""
    session = await sessions.get_session(token, settings)
    username = (session or {}).get("username")
    if not session or not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return UserOut(username=str(username))


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> UserOut:
    return await resolve_session(token, settings)
