"""公开数据契约。这些类型同时是 API 的请求/响应模型与审计留痕的字段定义。"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    L0 = "L0"  # 免密小额
    L1 = "L1"  # 中额，复述 + 二次确认
    L2 = "L2"  # 大额，子女远程授权
    L3 = "L3"  # 高风险，直接阻断


class Intent(BaseModel):
    name: str = "unknown"
    slots: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    raw_utterance: str = ""
    speaker: str = "elder"
    dialect: Literal["mandarin", "cantonese"] = "mandarin"
    source: Literal["rule", "llm"] = "rule"


class PlanStep(BaseModel):
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""


class Plan(BaseModel):
    intent: Intent
    steps: list[PlanStep] = Field(default_factory=list)
    readback: str = ""
    requires_auth: bool = False
    risk_hint: str | None = None


class RiskDecision(BaseModel):
    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    blocked: bool = False
    injection_hits: list[str] = Field(default_factory=list)


class AuthTicket(BaseModel):
    id: str
    amount: float
    payee: str
    payee_account: str = ""
    reason: str = ""
    status: Literal["pending", "approved", "rejected", "used", "expired"] = "pending"
    approver: str | None = None
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None


class AuditEntry(BaseModel):
    ts: datetime
    stage: str
    actor: str
    note: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class Subscription(BaseModel):
    id: str
    merchant: str
    category: str
    monthly_amount: float
    next_charge: str
    suspicious: bool = False
    reason: str = ""
    status: Literal["active", "cancelled"] = "active"


class PersonaProfile(BaseModel):
    name: str = "张桂芬"
    elder_id: str = "E001"
    child_id: str = "C001"
    child_name: str = "张伟"
    dialect: Literal["mandarin", "cantonese"] = "mandarin"
    speech_rate: float = 0.8
    font_scale: float = 1.3
    secret_phrase: str = "桂花糕"
    free_limit: float = 200.0
    daily_limit: float = 50000.0


# ---- 请求体 ----


class PlanRequest(BaseModel):
    text: str
    dialect: Literal["auto", "mandarin", "cantonese"] = "auto"
    elder_id: str = "E001"
    speaker: str = "elder"
    hour: int | None = None
    device_trusted: bool = True
    voiceprint_ok: bool = True
    second_speaker: bool = False
    defense_enabled: bool = True
    # 外部不可信内容（商户名、账单描述、他人转发的文本），只作为数据，不得作为指令。
    untrusted_context: list[str] = Field(default_factory=list)


class ConfirmRequest(BaseModel):
    session_id: str
    ack_voice: bool = False
    ack_popup: bool = False
    hesitation_ms: int = 0


class ExecuteRequest(BaseModel):
    session_id: str


class AsrRequest(BaseModel):
    dialect: Literal["mandarin", "cantonese"] = "mandarin"
    audio_b64: str | None = None
    transcript_hint: str | None = None


class TtsRequest(BaseModel):
    text: str
    dialect: Literal["mandarin", "cantonese"] = "mandarin"
    rate: float = 0.8


class RiskRequest(BaseModel):
    amount: float = 0.0
    payee: str = ""
    hour: int | None = None
    device_trusted: bool = True
    voiceprint_ok: bool = True
    second_speaker: bool = False
    hesitation_count: int = 0
    utterance: str = ""
    defense_enabled: bool = True


class AuthApproveRequest(BaseModel):
    ticket_id: str
    approver: str = "张伟"
    amount: float | None = None
    payee: str | None = None
    ttl_seconds: int = 300
    approve: bool = True


class AttackRequest(BaseModel):
    script_id: str


class SubscriptionCancelRequest(BaseModel):
    subscription_id: str


class DeployPlanRequest(BaseModel):
    """子女端远程预置的守护日历动作链。"""

    event: str
    lock_amount: float = 1000.0
    advance_days: int = 2
    actions: list[str] = Field(default_factory=lambda: ["订购鲜花", "订购蛋糕"])


class EmergencyRequest(BaseModel):
    reason: str = "用户触发紧急止付暗语"


def default_expiry(ttl_seconds: int = 300) -> datetime:
    return datetime.now() + timedelta(seconds=ttl_seconds)
