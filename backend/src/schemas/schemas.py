from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator

# bcrypt учитывает только первые 72 байта пароля
MAX_PASSWORD_BYTES = 72


class UserBase(BaseModel):
    username: str


class UserCreate(UserBase):
    password: str

    @field_validator("username")
    @classmethod
    def check_username(cls, value: str) -> str:
        value = value.strip()
        if not 3 <= len(value) <= 32:
            raise ValueError("Username must be 3 to 32 characters long")
        if any(char.isspace() for char in value):
            raise ValueError("Username must not contain spaces")
        return value

    @field_validator("password")
    @classmethod
    def check_password(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes long")
        return value


class User(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    avatar_url: Optional[str] = None


class Token(BaseModel):
    access_token: str
    token_type: str


class RegisterResponse(Token):
    message: str


class TokenData(BaseModel):
    username: Optional[str] = None
