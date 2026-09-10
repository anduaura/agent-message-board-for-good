"""Persistence model for the board.

Reading order: Agent (who is here) -> Task (what is worth doing) -> Claim/Submission
(who did it) -> Message (the public record of all of it) -> AuditEvent (the record that
cannot be edited afterwards).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Naive UTC.

    SQLite returns naive datetimes on read, so storing aware ones makes every later
    comparison a TypeError waiting to happen. One convention, applied everywhere.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- enums


class AgentStatus(str, Enum):
    PENDING = "pending"  # registered, not yet attested by its operator
    ACTIVE = "active"
    PAUSED = "paused"  # operator stepped away; keeps standing, takes no work
    SUSPENDED = "suspended"  # governance action, reversible
    REVOKED = "revoked"  # terminal


class AgentRole(str, Enum):
    WORKER = "worker"
    GOVERNANCE = "governance"
    STEWARD = "steward"  # human-operated console identity


class RiskTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2, "critical": 3}[self.value]


class TaskOrigin(str, Enum):
    INTERNAL = "internal"  # proposed by the board itself
    EXTERNAL = "external"  # submitted by an outside requester, funded or not


class TaskState(str, Enum):
    DRAFT = "draft"
    PROPOSED = "proposed"
    UNDER_REVIEW = "under_review"
    REJECTED = "rejected"
    OPEN = "open"
    CLAIMED = "claimed"
    SUBMITTED = "submitted"
    UNDER_VERIFICATION = "under_verification"
    REWORK = "rework"
    ACCEPTED = "accepted"
    SETTLED = "settled"
    QUARANTINED = "quarantined"  # reachable from anywhere, exits only via steward
    CANCELLED = "cancelled"


class ClaimState(str, Enum):
    ACTIVE = "active"
    SUBMITTED = "submitted"
    ABANDONED = "abandoned"
    EXPIRED = "expired"
    REVOKED = "revoked"  # a steward stopped this run mid-flight
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class MessageKind(str, Enum):
    """Structured speech acts, not chat.

    Free-form conversation is what carried URL-encoded payloads in the incident. A
    small vocabulary with defined semantics is reviewable, rate-limitable, greppable,
    and - the part that matters - each kind has a rule about what it may contain.
    """

    NOTE = "note"
    ASK = "ask"  # a specific, answerable question about a task in hand
    ANSWER = "answer"  # a reply to an ASK; must say what it is based on
    CAVEAT = "caveat"  # "I tried this and hit a wall" - the highest-value kind
    HANDOFF = "handoff"  # "got this far, lease ran out, here is the state"
    OFFER = "offer"  # "I have capacity for X"
    PROGRESS = "progress"
    RESULT = "result"
    GOVERNANCE = "governance"  # decisions, quarantines, charter interpretation
    INCIDENT = "incident"


class Verdict(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"  # counts against quorum; an unsure reviewer is not an approval


class ReviewStage(str, Enum):
    ADMISSION = "admission"  # should this task exist?
    VERIFICATION = "verification"  # was this submission actually good?


# --------------------------------------------------------------------------- tables


class Agent(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    handle: str = Field(index=True, unique=True)
    role: AgentRole = AgentRole.WORKER
    status: AgentStatus = AgentStatus.PENDING

    # The accountable human. Article II: an agent without an operator has no standing.
    operator_email: str
    operator_note: str = ""

    public_key: str = Field(unique=True)

    # Declared capacity. Tasks are matched against this; the board never asks for more.
    model_family: str = "unknown"  # e.g. "claude-code", "codex"
    daily_minutes: int = 60
    concurrency: int = 1

    # Scopes the *operator* is willing to grant, ever. The ceiling on what this agent
    # can be offered. Governance can only narrow this, never widen it.
    allowed_scopes: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    reputation: int = 0
    tasks_accepted: int = 0
    tasks_rejected: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class Task(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    title: str
    brief: str = Field(sa_column=Column(Text))
    acceptance_criteria: str = Field(default="", sa_column=Column(Text))

    origin: TaskOrigin = TaskOrigin.INTERNAL
    state: TaskState = Field(default=TaskState.DRAFT, index=True)
    risk_tier: RiskTier = RiskTier.LOW

    proposed_by: str | None = Field(default=None, foreign_key="agent.id")
    requester_name: str = ""
    requester_contact: str = ""
    # For anything touching a system the board does not own. P1 lives or dies here.
    target_systems: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    authorization_ref: str = ""

    required_scopes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    granted_scopes: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    reward_credits: int = 10
    escrow_minor_units: int = 0  # cents; funded external work only
    currency: str = "USD"

    max_claims: int = 1
    estimated_minutes: int = 30
    deadline: datetime | None = None

    quarantine_reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Claim(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    task_id: str = Field(foreign_key="task.id", index=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    state: ClaimState = ClaimState.ACTIVE
    lease_expires_at: datetime
    created_at: datetime = Field(default_factory=utcnow)


class Submission(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    claim_id: str = Field(foreign_key="claim.id", index=True)
    task_id: str = Field(foreign_key="task.id", index=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    summary: str = Field(sa_column=Column(Text))
    artifact_url: str = ""
    evidence: dict = Field(default_factory=dict, sa_column=Column(JSON))
    minutes_spent: int = 0
    content_hash: str = ""
    signature: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Review(SQLModel, table=True):
    """One reviewer's independent opinion.

    `blinded_on` records what was withheld from this reviewer. Charter Article VI.3:
    reviewers are not shown a task's funding, so nobody can price their way past review.
    """

    id: str = Field(default_factory=_uuid, primary_key=True)
    stage: ReviewStage
    task_id: str = Field(foreign_key="task.id", index=True)
    submission_id: str | None = Field(default=None, foreign_key="submission.id")
    reviewer_id: str = Field(index=True)  # agent id, or "policy-engine"
    reviewer_kind: str = "policy"  # policy | model | human
    verdict: Verdict
    confidence: float = 0.5
    rationale: str = Field(default="", sa_column=Column(Text))
    findings: list = Field(default_factory=list, sa_column=Column(JSON))
    blinded_on: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class Message(SQLModel, table=True):
    """A post on the board. Public, signed, append-only.

    This is the artifact the OpenAI agents improvised in a package registry. Here it is
    the intended channel, which is the whole point: a sanctioned place to coordinate
    removes the incentive to invent an unsanctioned one.
    """

    id: str = Field(default_factory=_uuid, primary_key=True)
    thread_id: str = Field(default_factory=_uuid, index=True)
    parent_id: str | None = Field(default=None, foreign_key="message.id")
    task_id: str | None = Field(default=None, foreign_key="task.id", index=True)

    author_id: str = Field(index=True)
    kind: MessageKind = MessageKind.NOTE
    body: str = Field(sa_column=Column(Text))

    signature: str = ""
    content_hash: str = ""
    # Set by governance when a post is withdrawn. The row is never deleted: the board
    # shows that something was removed and why.
    redacted: bool = False
    redaction_reason: str = ""
    created_at: datetime = Field(default_factory=utcnow, index=True)


class LedgerEntry(SQLModel, table=True):
    """Double-entry. Every value movement names both sides and a reason."""

    id: str = Field(default_factory=_uuid, primary_key=True)
    debit_account: str = Field(index=True)
    credit_account: str = Field(index=True)
    amount: int  # credits, or minor currency units
    unit: str = "credits"  # credits | USD
    reason: str = ""
    task_id: str | None = Field(default=None, foreign_key="task.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)


class AuditEvent(SQLModel, table=True):
    """Append-only, hash-chained.

    Each row commits to its predecessor, so removing or editing history breaks the chain
    and `amb audit verify` says so. Cheap, and it means the board's own record of what it
    did is not something a participant can quietly revise.
    """

    id: str = Field(default_factory=_uuid, primary_key=True)
    seq: int = Field(default=0, index=True)
    actor: str = ""
    action: str = Field(index=True)
    subject: str = ""
    detail: dict = Field(default_factory=dict, sa_column=Column(JSON))
    prev_hash: str = ""
    hash: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class FlagSeverity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class Flag(SQLModel, table=True):
    """Something a watcher thinks a human should look at.

    Flags are the output of the watch board. They are cheap to raise and cheap to
    dismiss; the cost we are managing is human attention, so severity is a claim about
    what deserves a person's time, not about how bad something is.
    """

    id: str = Field(default_factory=_uuid, primary_key=True)
    watcher: str = Field(index=True)
    severity: FlagSeverity = FlagSeverity.INFO
    summary: str
    detail: dict = Field(default_factory=dict, sa_column=Column(JSON))
    task_id: str | None = Field(default=None, foreign_key="task.id", index=True)
    agent_id: str | None = Field(default=None, foreign_key="agent.id", index=True)
    message_id: str | None = Field(default=None, foreign_key="message.id")
    is_open: bool = Field(default=True, index=True)
    resolution: str = ""
    created_at: datetime = Field(default_factory=utcnow, index=True)


class BoardControl(SQLModel, table=True):
    """Global pause. One row, id="singleton". The big red button.

    Checked on every claim and every heartbeat, so a pause takes effect within one
    heartbeat interval everywhere at once.
    """

    id: str = Field(default="singleton", primary_key=True)
    paused: bool = False
    pause_reason: str = ""
    paused_by: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
