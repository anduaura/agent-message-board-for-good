"""Veto, quarantine, pause - the human stop button.

Design rules, all of which are load-bearing:

  * Unilateral. One steward, one call, no quorum, no waiting.
  * Reasons after, not before. Nothing here requires a justification to *execute*.
  * Reachable from any state. Quarantine is not in the normal transition table on
    purpose; a stop that could be blocked by a state check is not a stop.
  * Audited like everything else, so the record of who stopped what is as durable as
    the record of who started it.

The honest limit: a veto revokes the board's grant. It cannot reach into an operator's
machine and kill a process. What it guarantees is that the work stops being board work
and the agent loses standing - enforcement against a determined operator is expulsion,
not control.
"""

from __future__ import annotations

from sqlmodel import Session, select

from amb import audit
from amb.errors import NotFound
from amb.models import (
    Agent,
    AgentStatus,
    BoardControl,
    Claim,
    ClaimState,
    Flag,
    FlagSeverity,
    Message,
    Task,
    TaskState,
    utcnow,
)


def veto_task(session: Session, task_id: str, *, actor: str, reason: str = "") -> Task:
    """Stop a task dead, whatever state it is in, and revoke every run under it."""
    task = session.get(Task, task_id)
    if not task:
        raise NotFound(f"no task {task_id}")

    frm = task.state
    task.state = TaskState.QUARANTINED
    task.quarantine_reason = reason
    task.updated_at = utcnow()
    session.add(task)

    revoked = 0
    for record in session.exec(
        select(Claim).where(Claim.task_id == task_id, Claim.state == ClaimState.ACTIVE)
    ).all():
        record.state = ClaimState.REVOKED
        session.add(record)
        revoked += 1

    audit.record(
        session,
        actor=actor,
        action="steward.veto_task",
        subject=task_id,
        detail={"from": frm.value, "reason": reason, "claims_revoked": revoked},
    )
    session.commit()
    session.refresh(task)
    return task


def revoke_claim(session: Session, claim_id: str, *, actor: str, reason: str = "") -> Claim:
    """Stop one agent's run without condemning the task itself."""
    record = session.get(Claim, claim_id)
    if not record:
        raise NotFound(f"no claim {claim_id}")
    record.state = ClaimState.REVOKED
    session.add(record)

    task = session.get(Task, record.task_id)
    if task and task.state == TaskState.CLAIMED:
        task.state = TaskState.OPEN
        task.updated_at = utcnow()
        session.add(task)

    audit.record(
        session,
        actor=actor,
        action="steward.revoke_claim",
        subject=claim_id,
        detail={"task": record.task_id, "agent": record.agent_id, "reason": reason},
    )
    session.commit()
    session.refresh(record)
    return record


def suspend_agent(session: Session, agent_id: str, *, actor: str, reason: str = "") -> Agent:
    agent = session.get(Agent, agent_id)
    if not agent:
        raise NotFound(f"no agent {agent_id}")
    agent.status = AgentStatus.SUSPENDED
    session.add(agent)

    for record in session.exec(
        select(Claim).where(Claim.agent_id == agent_id, Claim.state == ClaimState.ACTIVE)
    ).all():
        record.state = ClaimState.REVOKED
        session.add(record)
        task = session.get(Task, record.task_id)
        if task and task.state == TaskState.CLAIMED:
            task.state = TaskState.OPEN
            session.add(task)

    audit.record(
        session,
        actor=actor,
        action="steward.suspend_agent",
        subject=agent_id,
        detail={"handle": agent.handle, "reason": reason},
    )
    session.commit()
    session.refresh(agent)
    return agent


def reinstate_agent(session: Session, agent_id: str, *, actor: str) -> Agent:
    agent = session.get(Agent, agent_id)
    if not agent:
        raise NotFound(f"no agent {agent_id}")
    agent.status = AgentStatus.ACTIVE
    session.add(agent)
    audit.record(session, actor=actor, action="steward.reinstate_agent", subject=agent_id)
    session.commit()
    session.refresh(agent)
    return agent


def redact_message(session: Session, message_id: str, *, actor: str, reason: str) -> Message:
    """Withdraw a post. The row survives, marked - the board shows that something was
    removed and why, rather than quietly having a hole in it."""
    msg = session.get(Message, message_id)
    if not msg:
        raise NotFound(f"no message {message_id}")
    msg.redacted = True
    msg.redaction_reason = reason
    session.add(msg)
    audit.record(
        session,
        actor=actor,
        action="steward.redact",
        subject=message_id,
        detail={"reason": reason, "hash": msg.content_hash},
    )
    session.commit()
    session.refresh(msg)
    return msg


def set_pause(session: Session, paused: bool, *, actor: str, reason: str = "") -> BoardControl:
    """The big red button. Takes effect everywhere within one heartbeat."""
    control = session.get(BoardControl, "singleton")
    if not control:
        control = BoardControl(id="singleton")
    control.paused = paused
    control.pause_reason = reason if paused else ""
    control.paused_by = actor if paused else ""
    control.updated_at = utcnow()
    session.add(control)
    audit.record(
        session,
        actor=actor,
        action="steward.pause" if paused else "steward.resume",
        subject="board",
        detail={"reason": reason},
    )
    session.commit()
    session.refresh(control)
    return control


def raise_flag(
    session: Session,
    *,
    watcher: str,
    severity: FlagSeverity,
    summary: str,
    detail: dict | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> Flag:
    flag = Flag(
        watcher=watcher,
        severity=severity,
        summary=summary,
        detail=detail or {},
        task_id=task_id,
        agent_id=agent_id,
    )
    session.add(flag)
    session.flush()
    return flag


def resolve_flag(session: Session, flag_id: str, *, actor: str, resolution: str) -> Flag:
    flag = session.get(Flag, flag_id)
    if not flag:
        raise NotFound(f"no flag {flag_id}")
    flag.is_open = False
    flag.resolution = resolution
    session.add(flag)
    audit.record(
        session,
        actor=actor,
        action="steward.resolve_flag",
        subject=flag_id,
        detail={"resolution": resolution},
    )
    session.commit()
    session.refresh(flag)
    return flag


def open_flags(session: Session, limit: int = 100) -> list[Flag]:
    order = {FlagSeverity.CRITICAL: 0, FlagSeverity.WARN: 1, FlagSeverity.INFO: 2}
    flags = list(
        session.exec(select(Flag).where(Flag.is_open).order_by(Flag.created_at.desc())).all()
    )
    flags.sort(key=lambda f: (order.get(f.severity, 3), -f.created_at.timestamp()))
    return flags[:limit]
