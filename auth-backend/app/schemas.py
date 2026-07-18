from typing import Any

from pydantic import BaseModel, Field


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    username: str
    role: str = ""


class UserDetail(UserOut):
    created_at: str | None = None
    channels: list[dict[str, Any]] = Field(default_factory=list)


class ValidateRequest(BaseModel):
    token: str


class AuthorizeRequest(BaseModel):
    token: str
    service: str
    method: str
    path: str


class AuthorizeResponse(BaseModel):
    allowed: bool
    username: str
    role: str


class AccountRequestIn(BaseModel):
    username: str


class AccountRef(BaseModel):
    username: str


class Grant(BaseModel):
    service: str = "*"
    methods: list[str] = Field(default_factory=lambda: ["*"])
    path_prefix: str = "/"


class RoleIn(BaseModel):
    name: str
    description: str = ""
    grants: list[Grant] = Field(default_factory=list)


class RoleOut(BaseModel):
    name: str
    description: str = ""
    grants: list[Grant] = Field(default_factory=list)
    created_at: str | None = None


class CreateUserIn(BaseModel):
    username: str
    password: str
    role: str


class UpdateUserIn(BaseModel):
    password: str | None = None
    role: str | None = None


class ApproveAccountRequestIn(BaseModel):
    username: str
    password: str
    role: str


class NewUserIn(BaseModel):
    username: str
    password: str
    role: str


class AssignChannelIn(BaseModel):
    channel: str
    device_id: str
    # Assign to an existing user...
    username: str | None = None
    # ...or create a new one in the same step.
    new_user: NewUserIn | None = None


class ChannelRef(BaseModel):
    channel: str
    device_id: str


class OpenClawSessionIn(BaseModel):
    channel: str
    device_id: str
    display_name: str = ""


class OpenClawPairingRequestIn(BaseModel):
    """DM pairing request reported by the funnelmanager-auth plugin."""

    channel: str
    device_id: str
    code: str
    display_name: str = ""


class OpenClawSessionOut(Token):
    username: str
    role: str
    expires_in: int


class AppLink(BaseModel):
    name: str
    description: str = ""
    url: str
