"""Pydantic request/response schemas. Field names match the frontend types."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator


# --- auth ---------------------------------------------------------------


class UserOut(BaseModel):
    id: str
    name: str
    email: str


class RegisterPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required.")
        return v


class LoginPayload(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class AuthResponse(BaseModel):
    token: str
    user: UserOut


# --- chat ---------------------------------------------------------------


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    createdAt: datetime


class SessionOut(BaseModel):
    id: str
    title: str
    updatedAt: datetime
    messages: list[MessageOut] = []


class SessionSummary(BaseModel):
    id: str
    title: str
    updatedAt: datetime


class CreateSessionPayload(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class RenameSessionPayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ChatPayload(BaseModel):
    content: str = Field(min_length=1, max_length=16000)

    @field_validator("content")
    @classmethod
    def strip_content(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Message cannot be empty.")
        return v


class ChatResponse(BaseModel):
    session: SessionSummary
    userMessage: MessageOut
    assistantMessage: MessageOut


class ModelStatus(BaseModel):
    backend: str
    model: str | None = None
    loaded: bool
    contextSize: int
    ramGb: float
    tier: str
    # idle | downloading | loading | ready | error
    status: str = "idle"
    detail: str | None = None
    threads: int = 1
