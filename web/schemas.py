from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str
    message: str


class Envelope(BaseModel, Generic[T]):
    ok: bool = True
    data: T | None = None
    error: ErrorDetail | None = None


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    token: str
    expires_at: float


class RecoverRequest(BaseModel):
    """密码找回请求（无鉴权）：验证找回答案后重置 WEBUI_PASSWORD。"""
    answer: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    """修改登录密码请求（需鉴权）：必须通过找回答案验证。"""
    old_password: str = ""
    new_password: str
    answer: str
    new_question: str = ""
    new_answer: str = ""


class ChatRequest(BaseModel):
    session_id: str = ""
    agent: str = "xiaoda"
    text: str = Field(..., max_length=50_000)
    image_data: list | None = None


class SessionInfo(BaseModel):
    session_id: str
    title: str
    last_message: str
    message_count: int
    created_at: float
    updated_at: float
    source: str = "web"


class MessageItem(BaseModel):
    id: int
    role: str
    content: str
    emotion: str | None = None
    timestamp: float
    tool_calls: list | None = None
    request_context: dict | None = None


class SystemStatus(BaseModel):
    uptime: float
    qq_connected: bool
    wechat_connected: bool = False
    active_sessions: int
    version: str
    permission_mode: str


class SlashCommand(BaseModel):
    name: str
    description: str
    owner_only: bool


class AgentBrief(BaseModel):
    name: str
    display_name: str
    enabled: bool
    model: str
    provider: str
