"""Admin-gated management routes: users, roles, account requests.

Every route here runs through ``require_authorized`` — token validation plus
an OPA check with service="auth", so only roles whose grants cover the auth
service (the built-in ``admin`` role covers everything) can touch them.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status

from app import opa, store
from app.schemas import (
    AccountRef,
    ApproveAccountRequestIn,
    CreateUserIn,
    RoleIn,
    RoleOut,
    UpdateUserIn,
    UserDetail,
    UserOut,
)
from app.security import hash_password, require_authorized

router = APIRouter(
    prefix="/api/auth/admin",
    tags=["auth-admin"],
    dependencies=[Depends(require_authorized)],
)

# The auth service is a single asyncio process, but check-then-act sequences
# (e.g. the last-admin guard, which reads a count then mutates) still interleave
# across concurrent requests at await points. Serialize profile mutations so a
# guard and its write are atomic relative to other admin writes.
_write_lock = asyncio.Lock()


async def _require_role_exists(role: str) -> str:
    role = (role or "").strip().lower()
    if not await store.get_role(role):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Role "{role}" does not exist',
        )
    return role


async def _user_detail(user: dict) -> UserDetail:
    return UserDetail(
        username=str(user["username"]),
        role=str(user.get("role") or ""),
        created_at=user.get("created_at"),
    )


async def _guard_last_admin(username: str) -> None:
    """Refuse to delete/demote the only remaining admin-role user."""
    user = await store.get_user(username)
    if not user or user.get("role") != store.ADMIN_ROLE:
        return
    if await store.count_users_with_role(store.ADMIN_ROLE) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove the last admin user",
        )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[UserDetail])
async def list_users() -> list[UserDetail]:
    return [await _user_detail(user) for user in await store.list_users()]


@router.post("/users", response_model=UserDetail, status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUserIn) -> UserDetail:
    username = store.normalize_username(body.username)
    if not store.valid_username(username):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Username must be 3-32 chars: lowercase letters, digits, . _ -",
        )
    if await store.get_user(username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'User "{username}" already exists',
        )
    if not body.password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password is required",
        )
    role = await _require_role_exists(body.role)
    user = await store.create_user(username, hash_password(body.password), role)
    await store.delete_account_request(username)
    return await _user_detail(user)


@router.patch("/users/{username}", response_model=UserDetail)
async def update_user(username: str, body: UpdateUserIn) -> UserDetail:
    async with _write_lock:
        user = await store.get_user(username)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        fields: dict[str, str] = {}
        if body.role is not None:
            role = await _require_role_exists(body.role)
            if role != user.get("role"):
                await _guard_last_admin(username)
            fields["role"] = role
        if body.password is not None:
            if not body.password:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Password cannot be empty",
                )
            fields["password_hash"] = hash_password(body.password)
        updated = await store.update_user(username, **fields)
        assert updated is not None
        return await _user_detail(updated)


@router.delete("/users/{username}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    username: str, current_user: UserOut = Depends(require_authorized)
) -> None:
    if store.normalize_username(username) == current_user.username:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete your own account",
        )
    async with _write_lock:
        await _guard_last_admin(username)
        if not await store.delete_user(username):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@router.get("/roles", response_model=list[RoleOut])
async def list_roles() -> list[RoleOut]:
    return [RoleOut(**role) for role in await store.list_roles()]


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(body: RoleIn) -> RoleOut:
    name = (body.name or "").strip().lower()
    if not store.valid_role_name(name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Role name must be 2-32 chars: lowercase letters, digits, _ -",
        )
    if await store.get_role(name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Role "{name}" already exists',
        )
    role = await store.save_role(
        name, body.description, [grant.model_dump() for grant in body.grants]
    )
    await opa.push_roles_data()
    return RoleOut(**role)


@router.delete("/roles/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(name: str) -> None:
    name = (name or "").strip().lower()
    if name == store.ADMIN_ROLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The admin role cannot be deleted",
        )
    if await store.count_users_with_role(name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role is still assigned to users",
        )
    if not await store.delete_role(name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    await opa.push_roles_data()


# ---------------------------------------------------------------------------
# Account requests
# ---------------------------------------------------------------------------


@router.get("/account-requests")
async def list_account_requests() -> list[dict]:
    return await store.list_account_requests()


@router.post("/account-requests/approve", response_model=UserDetail)
async def approve_account_request(body: ApproveAccountRequestIn) -> UserDetail:
    username = store.normalize_username(body.username)
    if not store.valid_username(username):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Username must be 3-32 chars: lowercase letters, digits, . _ -",
        )
    if await store.get_user(username):
        await store.delete_account_request(username)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'User "{username}" already exists',
        )
    role = await _require_role_exists(body.role)
    if not body.password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password is required",
        )
    user = await store.create_user(username, hash_password(body.password), role)
    await store.delete_account_request(username)
    return await _user_detail(user)


@router.post("/account-requests/deny", status_code=status.HTTP_204_NO_CONTENT)
async def deny_account_request(body: AccountRef) -> None:
    if not await store.delete_account_request(body.username):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
