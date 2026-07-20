import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import opa, store
from app.config import get_settings
from app.routers import admin, auth
from app.security import hash_or_keep
from app.sessions import close_redis, get_redis

logger = logging.getLogger(__name__)


async def bootstrap() -> None:
    """Ensure the built-in admin role and the bootstrap admin user exist.

    The admin user is created from AUTH_USERNAME/AUTH_PASSWORD only when
    missing — existing users are never overwritten from env. Everything else
    (more users, roles) is managed through the admin API/UI.
    """
    settings = get_settings()
    if not await store.get_role(store.ADMIN_ROLE):
        await store.save_role(
            store.ADMIN_ROLE,
            "Full access to every service",
            [{"service": "*", "methods": ["*"], "path_prefix": "/"}],
        )
        logger.info("Created built-in role %r", store.ADMIN_ROLE)
    username = store.normalize_username(settings.auth_username)
    if username and not await store.get_user(username):
        await store.create_user(
            username, hash_or_keep(settings.auth_password), store.ADMIN_ROLE
        )
        logger.info("Created bootstrap admin user %r", username)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await get_redis().ping()
        await bootstrap()
    except Exception:
        logger.exception("Redis unavailable at startup; sessions/profiles degraded until it recovers")
    # Keep OPA loaded with the policy + role data (self-heals after OPA restarts).
    reconcile = asyncio.create_task(opa.reconcile_loop())
    yield
    reconcile.cancel()
    await close_redis()


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(admin.router)
# Health lives at /api/auth/health (auth.router) so every public route stays
# under the service's /api/auth prefix.
