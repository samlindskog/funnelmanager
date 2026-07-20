"""Admin-gated management routes: users, roles, account + channel requests.

Every route here runs through ``require_authorized`` — token validation plus
an OPA check with service="auth", so only roles whose grants cover the auth
service (the built-in ``admin`` role covers everything) can touch them.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status

from app import opa, openclaw_gateway, store
from app.routers import internal
from app.schemas import (
    AccountRef,
    ApproveAccountRequestIn,
    AssignChannelIn,
    ChannelRef,
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
        channels=await store.channels_for_user(str(user["username"])),
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
        # Strip the user's OpenClaw peer bindings and revoke their senders' DM
        # pairing before dropping the channel links (the agent's workspace is
        # intentionally retained on disk). Runs even when no local links remain
        # — a partially-failed assign can leave gateway bindings without a
        # local link, and delete is the last chance to reconcile them. A
        # gateway failure aborts the delete so the admin can retry.
        target = store.normalize_username(username)
        if await store.get_user(target):
            links = await store.channels_for_user(target)
            await openclaw_gateway.sync_agent(
                target,
                [],
                removed=[
                    {"channel": link["channel"], "device_id": link["device_id"]}
                    for link in links
                ],
            )
        if not await store.delete_user(username):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")


@router.delete(
    "/users/{username}/channels/{channel}/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unlink_user_channel(username: str, channel: str, device_id: str) -> None:
    # The whole read-compute-sync-mutate sequence holds the write lock: the
    # gateway sync has full-sync semantics, so a concurrent link change for the
    # same user racing this read would silently drop its binding.
    async with _write_lock:
        link = await store.get_channel_link(channel, device_id)
        if not link or link.get("username") != store.normalize_username(username):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Channel link not found"
            )
        # Sync the user's OpenClaw agent bindings to the post-unlink sender set
        # and revoke the removed sender's DM pairing, before mutating local
        # state (a gateway failure aborts the unlink so the admin can retry).
        target = store.normalize_username(username)
        remaining = [
            {"channel": other["channel"], "device_id": other["device_id"]}
            for other in await store.channels_for_user(target)
            if not (
                other.get("channel") == link.get("channel")
                and other.get("device_id") == link.get("device_id")
            )
        ]
        await openclaw_gateway.sync_agent(
            target,
            remaining,
            removed=[
                {"channel": link["channel"], "device_id": link["device_id"]}
            ],
        )
        await store.unlink_channel(channel, device_id)
        # Revoke the channel's cached OpenClaw session so the sender loses
        # access now, not when the session TTL expires.
        await internal.revoke_channel_session(channel, device_id)


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


# ---------------------------------------------------------------------------
# Channel requests (unlinked channel device ids reported by OpenClaw)
# ---------------------------------------------------------------------------


@router.get("/channel-requests")
async def list_channel_requests() -> list[dict]:
    return await store.list_channel_requests()


async def _approve_pairing_if_pending(request: dict) -> None:
    """Complete the OpenClaw DM pairing step if the request still carries a
    pairing code. No-op otherwise."""
    code = str(request.get("pairing_code") or "")
    if not code:
        return
    await openclaw_gateway.approve_pairing(
        str(request.get("channel") or ""), code
    )
    await store.clear_channel_request_pairing(
        str(request.get("channel") or ""), str(request.get("device_id") or "")
    )


@router.post("/channel-requests/approve-pairing")
async def approve_channel_pairing(body: ChannelRef) -> dict:
    """Approve the OpenClaw DM pairing for a pending channel request (the
    sender can then talk to the agent; profile assignment is a separate step
    unless done via /assign, which also completes pairing)."""
    channel = (body.channel or "").strip().lower()
    device_id = str(body.device_id or "").strip()
    request = await store.get_channel_request(channel, device_id)
    if not request:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    if not request.get("pairing_code"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No pairing approval pending for this channel",
        )
    await _approve_pairing_if_pending(request)
    # If the channel is already linked to a profile, pairing was the only
    # outstanding step — drop the request row entirely.
    if await store.get_channel_link(channel, device_id):
        await store.delete_channel_request(channel, device_id)
        return {"channel": channel, "device_id": device_id, "paired": True, "linked": True}
    updated = await store.get_channel_request(channel, device_id) or {}
    return {**updated, "paired": True, "linked": False}


@router.post("/channel-requests/assign", response_model=UserDetail)
async def assign_channel_request(body: AssignChannelIn) -> UserDetail:
    channel = (body.channel or "").strip().lower()
    device_id = str(body.device_id or "").strip()
    if not channel or not device_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="channel and device_id are required",
        )
    if body.new_user:
        username = store.normalize_username(body.new_user.username)
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
        if not body.new_user.password:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Password is required",
            )
        role = await _require_role_exists(body.new_user.role)
        user = await store.create_user(username, hash_password(body.new_user.password), role)
        await store.delete_account_request(username)
    elif body.username:
        user = await store.get_user(body.username)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide username (existing user) or new_user",
        )
    request = await store.get_channel_request(channel, device_id) or {}
    # One-stop onboarding: if the OpenClaw DM pairing is still pending, approve
    # it first (failure aborts the assign, so the row keeps its code and the
    # admin can retry). Kept outside the write lock — it doesn't touch the
    # synced binding state.
    await _approve_pairing_if_pending(request)
    # The read-compute-sync-mutate sequence holds the write lock: the gateway
    # sync has full-sync semantics, so two concurrent assigns for the same user
    # would each read a stale sender set and the loser's binding would be
    # silently pruned; a racing user delete could resurrect the agent.
    async with _write_lock:
        user = await store.get_user(str(user["username"]))
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )
        # Sync the user's OpenClaw agent + peer bindings to the prospective
        # sender set (existing links plus the one being assigned) before
        # mutating local state — a gateway failure aborts the assign so the
        # admin can retry.
        prospective = {
            (str(other.get("channel") or ""), str(other.get("device_id") or ""))
            for other in await store.channels_for_user(str(user["username"]))
        }
        prospective.add((channel, device_id))
        await openclaw_gateway.sync_agent(
            str(user["username"]),
            [{"channel": c, "device_id": d} for c, d in sorted(prospective)],
        )
        # If the channel was previously linked to another user, kill that
        # user's cached OpenClaw session before re-linking.
        await internal.revoke_channel_session(channel, device_id)
        await store.link_channel(
            channel, device_id, str(user["username"]), str(request.get("display_name") or "")
        )
        await store.delete_channel_request(channel, device_id)
    return await _user_detail(user)


@router.post("/channel-requests/deny", status_code=status.HTTP_204_NO_CONTENT)
async def deny_channel_request(body: ChannelRef) -> None:
    if not await store.delete_channel_request(body.channel, body.device_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
