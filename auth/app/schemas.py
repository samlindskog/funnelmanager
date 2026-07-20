from pydantic import BaseModel, Field


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    username: str
    role: str = ""


class UserDetail(UserOut):
    created_at: str | None = None


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


class AppLink(BaseModel):
    name: str
    description: str = ""
    url: str
