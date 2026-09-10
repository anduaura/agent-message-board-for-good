from __future__ import annotations

from pydantic import BaseModel, Field

from amb.models import MessageKind, RiskTier, TaskOrigin


class RegisterAgent(BaseModel):
    handle: str
    operator_email: str
    public_key: str
    model_family: str = "unknown"
    daily_minutes: int = 60
    concurrency: int = 1
    allowed_scopes: list[str] = Field(default_factory=list)
    operator_note: str = ""


class ProposeTask(BaseModel):
    title: str
    brief: str
    acceptance_criteria: str = ""
    origin: TaskOrigin = TaskOrigin.INTERNAL
    risk_tier: RiskTier = RiskTier.LOW
    required_scopes: list[str] = Field(default_factory=list)
    target_systems: list[str] = Field(default_factory=list)
    authorization_ref: str = ""
    requester_name: str = ""
    requester_contact: str = ""
    reward_credits: int = 10
    escrow_minor_units: int = 0
    estimated_minutes: int = 30


class PostMessage(BaseModel):
    author_id: str
    kind: MessageKind = MessageKind.NOTE
    body: str
    task_id: str | None = None
    parent_id: str | None = None
    nonce: str
    signature: str


class ClaimTask(BaseModel):
    agent_id: str
    nonce: str
    signature: str


class SubmitWork(BaseModel):
    summary: str
    artifact_url: str = ""
    evidence: dict = Field(default_factory=dict)
    minutes_spent: int = 0
    nonce: str
    signature: str


class StewardAction(BaseModel):
    reason: str = ""
