"""Password hashing and the session-token dependencies for the auth service."""

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext

from app import opa, sessions, store
from app.config import Settings, get_settings
from app.schemas import UserOut

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# Precomputed bcrypt hash of a random string, verified on the login
# user-miss path so a missing username costs roughly the same as a wrong
# password — closes the timing oracle that would otherwise reveal which
# usernames exist. Value is meaningless; only the verify cost matters.
_DUMMY_HASH = "$2b$12$7qdL8kZniFgj51Hoc7g92.NrmF.ba2xPvPhAx8SErrAFQNXUfBZl."

_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def hash_password(plain_password: str) -> str:
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except ValueError:
        return False


def hash_or_keep(password: str) -> str:
    """Accept either a plain password (hash it) or a pre-made bcrypt hash.

    Only for the AUTH_PASSWORD *bootstrap* value, where letting operators
    supply a bcrypt hash is intentional. API-supplied passwords must always go
    through ``hash_password`` — otherwise a user password that happens to start
    with ``$2`` would be stored verbatim and could never be verified.
    """
    if password.startswith("$2"):
        return password
    return hash_password(password)


async def authenticate_user(username: str, password: str) -> dict | None:
    """Check credentials against the profile store; return the user on success."""
    user = await store.get_user(username)
    if not user:
        # Spend a comparable amount of time so the response latency does not
        # reveal whether the username exists.
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, str(user.get("password_hash") or "")):
        return None
    return user


async def resolve_session(token: str, settings: Settings) -> UserOut:
    """Session token -> live profile. 401 if the session or user is gone.

    Role is read from the user store on every call, so role changes and user
    deletion take effect immediately for existing sessions.
    """
    session = await sessions.get_session(token, settings)
    username = (session or {}).get("username")
    if not session or not username:
        raise _CREDENTIALS_EXC
    user = await store.get_user(str(username))
    if not user:
        raise _CREDENTIALS_EXC
    return UserOut(username=str(user["username"]), role=str(user.get("role") or ""))


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> UserOut:
    return await resolve_session(token, settings)


async def require_authorized(
    request: Request,
    current_user: UserOut = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> UserOut:
    """OPA-gated dependency for the auth service's own management routes."""
    try:
        allowed = await opa.authorize(
            username=current_user.username,
            role=current_user.role,
            service="auth",
            method=request.method,
            path=request.url.path,
            settings=settings,
        )
    except opa.OpaUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authorization service unavailable",
        ) from exc
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized",
        )
    return current_user
