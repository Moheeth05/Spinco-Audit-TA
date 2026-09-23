from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str
    role: str
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserCreateRequest(BaseModel):
    name: str
    email: str
    role: str
    password: str


class UserUpdateRequest(BaseModel):
    name: str | None = None
    email: str | None = None


class UserRoleChangeRequest(BaseModel):
    role: str


class UserStatusChangeRequest(BaseModel):
    is_active: bool


class AdminSetPasswordRequest(BaseModel):
    new_password: str
