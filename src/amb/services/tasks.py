"""Task lifecycle.

The legal transitions are declared as data at the top of this file rather than scattered
through the functions below. It means the state machine can be read in one screen, and
`transition()` is the only place a task's state ever changes - so quarantine, veto and
expiry cannot accidentally acquire their own back doors.
"""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, select

from amb import audit, crypto, policy, protocol
from amb.config import get_settings
from amb.errors import AMBError, IllegalTransition, NotFound, ScopeDenied
from amb.models import (
    Agent,
    AgentStatus,
    BoardControl,
    Claim,
    ClaimState,
    Review,
    ReviewStage,
    RiskTier,
    Submission,
    Task,
    TaskState,
    Verdict,
    utcnow,
)

# The whole state machine, as data.
TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.DRAFT: {TaskState.PROPOSED, TaskState.CANCELLED},
    TaskState.PROPOSED: {TaskState.UNDER_REVIEW, TaskState.CANCELLED},
    TaskState.UNDER_REVIEW: {TaskState.OPEN, TaskState.REJECTED},
    TaskState.OPEN: {TaskState.CLAIMED, TaskState.CANCELLED, TaskState.REJECTED},
    TaskState.CLAIMED: {TaskState.SUBMITTED, TaskState.OPEN, TaskState.CANCELLED},
    TaskState.SUBMITTED: {TaskState.UNDER_VERIFICATION},
    TaskState.UNDER_VERIFICATION: {TaskState.ACCEPTED, TaskState.REWORK, TaskState.REJECTED},
    TaskState.REWORK: {TaskState.OPEN, TaskState.CANCELLED},
    TaskState.ACCEPTED: {TaskState.SETTLED},
    TaskState.SETTLED: set(),
    TaskState.REJECTED: set(),
    TaskState.CANCELLED: set(),
    # Terminal for agents. Only a human steward reopens a quarantined task, and
    # reopening is itself an audited act.
    TaskState.QUARANTINED: set(),
}


def transition(
    session: Session, task: Task, to: TaskState, *, actor: str, reason: str = ""
) -> Task:
    if to not in TRANSITIONS.get(task.state, set()):
        raise IllegalTransition(f"cannot move task from {task.state.value} to {to.value}")
    frm = task.state
    task.state = to
    task.updated_at = utcnow()
    session.add(task)
    audit.record(
        session,
        actor=actor,
        action="task.transition",
        subject=task.id,
        detail={"from": frm.value, "to": to.value, "reason": reason},
    )
    return task


# --------------------------------------------------------------------------- proposing


def propose(session: Session, task: Task, *, actor: str) -> Task:
    task.state = TaskState.DRAFT
    session.add(task)
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="task.propose",
        subject=task.id,
        detail={"title": task.title, "origin": task.origin.value},
    )
    transition(session, task, TaskState.PROPOSED, actor=actor)
    session.commit()
    session.refresh(task)
    return task


def admit(session: Session, task: Task, *, actor: str = "policy-engine") -> tuple[Task, Review]:
    """Run admission review.

    v0 is the deterministic layer only, which is strictly the conservative choice: every
    judgement call becomes a rejection rather than an approval. Model reviewers slot in
    at this seam (M3) and can only ever *narrow* what the policy engine already allowed.
    """
    transition(session, task, TaskState.UNDER_REVIEW, actor=actor)
    result = policy.review_task(task)

    verdict = Verdict.APPROVE if result.admissible else Verdict.REJECT
    review = Review(
        stage=ReviewStage.ADMISSION,
        task_id=task.id,
        reviewer_id=actor,
        reviewer_kind="policy",
        verdict=verdict,
        confidence=1.0,  # deterministic: it is certain about exactly what it checked
        rationale=result.summary(),
        findings=result.as_list(),
        # Article VI.3 - reviewers never see what a task is worth.
        blinded_on=["escrow_minor_units", "reward_credits", "requester_name"],
    )
    session.add(review)

    if result.admissible:
        # Grant exactly what was asked for and nothing more. This is where "minimum
        # necessary scope" stops being a principle and becomes a column.
        task.granted_scopes = list(task.required_scopes)
        transition(session, task, TaskState.OPEN, actor=actor, reason=result.summary())
    else:
        transition(session, task, TaskState.REJECTED, actor=actor, reason=result.summary())

    session.commit()
    session.refresh(task)
    session.refresh(review)
    return task, review


# --------------------------------------------------------------------------- claiming


def board_paused(session: Session) -> BoardControl:
    control = session.get(BoardControl, "singleton")
    if not control:
        control = BoardControl(id="singleton")
        session.add(control)
        session.commit()
        session.refresh(control)
    return control


def open_tasks(session: Session) -> list[Task]:
    return list(
        session.exec(
            select(Task).where(Task.state == TaskState.OPEN).order_by(Task.created_at)
        ).all()
    )


def claim(session: Session, *, task_id: str, agent_id: str, signature: str, nonce: str) -> Claim:
    control = board_paused(session)
    if control.paused:
        raise AMBError(f"board is paused: {control.pause_reason or 'no reason given'}")

    task = session.get(Task, task_id)
    if not task:
        raise NotFound(f"no task {task_id}")
    agent = session.get(Agent, agent_id)
    if not agent:
        raise NotFound(f"no agent {agent_id}")
    if agent.status != AgentStatus.ACTIVE:
        raise AMBError(f"agent is {agent.status.value} and cannot claim work")
    if task.state != TaskState.OPEN:
        raise IllegalTransition(f"task is {task.state.value}, not open")

    crypto.verify(
        agent.public_key,
        protocol.claim_payload(agent_id=agent_id, task_id=task_id, nonce=nonce),
        signature,
    )

    # The operator's ceiling wins. The board narrows; it never widens.
    missing = sorted(set(task.granted_scopes) - set(agent.allowed_scopes))
    if missing:
        raise ScopeDenied(
            f"{agent.handle}'s operator has not allowed the scopes this task needs: {missing}"
        )
    if task.estimated_minutes > agent.daily_minutes:
        raise AMBError(
            f"task needs {task.estimated_minutes}min but {agent.handle} declared only "
            f"{agent.daily_minutes}min/day"
        )

    active = session.exec(
        select(Claim).where(Claim.agent_id == agent_id, Claim.state == ClaimState.ACTIVE)
    ).all()
    if len(active) >= agent.concurrency:
        raise AMBError(f"{agent.handle} is already at its declared concurrency limit")

    lease = get_settings().claim_lease_minutes
    record = Claim(
        task_id=task_id,
        agent_id=agent_id,
        lease_expires_at=utcnow() + timedelta(minutes=lease),
    )
    session.add(record)
    session.flush()
    transition(session, task, TaskState.CLAIMED, actor=agent_id)
    audit.record(
        session,
        actor=agent_id,
        action="claim.open",
        subject=record.id,
        detail={"task": task_id, "scopes": task.granted_scopes, "lease_minutes": lease},
    )
    session.commit()
    session.refresh(record)
    return record


def heartbeat(session: Session, claim_id: str) -> dict:
    """Worker liveness check, and the channel a veto travels down.

    Returns a status the worker acts on immediately. `stop` means stop - the worker does
    not get to weigh it against how close it thinks it is to finishing.
    """
    record = session.get(Claim, claim_id)
    if not record:
        raise NotFound(f"no claim {claim_id}")

    control = board_paused(session)
    if control.paused:
        return {"status": "stop", "reason": f"board paused: {control.pause_reason}"}
    if record.state == ClaimState.REVOKED:
        return {"status": "stop", "reason": "this claim was revoked by a steward"}
    if record.state != ClaimState.ACTIVE:
        return {"status": "stop", "reason": f"claim is {record.state.value}"}

    task = session.get(Task, record.task_id)
    if task and task.state == TaskState.QUARANTINED:
        return {"status": "stop", "reason": "task quarantined"}
    if utcnow() > record.lease_expires_at:
        record.state = ClaimState.EXPIRED
        session.add(record)
        if task and task.state == TaskState.CLAIMED:
            transition(session, task, TaskState.OPEN, actor="system", reason="lease expired")
        session.commit()
        return {"status": "stop", "reason": "lease expired"}

    remaining = int((record.lease_expires_at - utcnow()).total_seconds())
    return {"status": "ok", "lease_seconds_remaining": remaining,
            "granted_scopes": task.granted_scopes if task else []}


# ------------------------------------------------------------------------- submitting


def submit(
    session: Session,
    *,
    claim_id: str,
    summary: str,
    artifact_url: str = "",
    evidence: dict | None = None,
    minutes_spent: int = 0,
    signature: str,
    nonce: str,
) -> Submission:
    record = session.get(Claim, claim_id)
    if not record:
        raise NotFound(f"no claim {claim_id}")
    if record.state != ClaimState.ACTIVE:
        raise IllegalTransition(f"claim is {record.state.value}")

    agent = session.get(Agent, record.agent_id)
    task = session.get(Task, record.task_id)
    payload = protocol.submission_payload(
        agent_id=record.agent_id,
        claim_id=claim_id,
        summary=summary,
        artifact_url=artifact_url,
        nonce=nonce,
    )
    crypto.verify(agent.public_key, payload, signature)

    sub = Submission(
        claim_id=claim_id,
        task_id=record.task_id,
        agent_id=record.agent_id,
        summary=summary,
        artifact_url=artifact_url,
        evidence=evidence or {},
        minutes_spent=minutes_spent,
        signature=signature,
        content_hash=crypto.content_hash(payload),
    )
    session.add(sub)
    record.state = ClaimState.SUBMITTED
    session.add(record)
    session.flush()
    transition(session, task, TaskState.SUBMITTED, actor=record.agent_id)
    transition(session, task, TaskState.UNDER_VERIFICATION, actor="system")
    audit.record(
        session,
        actor=record.agent_id,
        action="submission.create",
        subject=sub.id,
        detail={"task": task.id, "hash": sub.content_hash, "minutes": minutes_spent},
    )
    session.commit()
    session.refresh(sub)
    return sub


def decide(
    session: Session, *, submission_id: str, accept: bool, actor: str, rationale: str = ""
) -> Task:
    """Verification outcome. In v0 a human steward decides; M3 adds reviewers."""
    sub = session.get(Submission, submission_id)
    if not sub:
        raise NotFound(f"no submission {submission_id}")
    task = session.get(Task, sub.task_id)
    record = session.get(Claim, sub.claim_id)
    agent = session.get(Agent, sub.agent_id)

    session.add(
        Review(
            stage=ReviewStage.VERIFICATION,
            task_id=task.id,
            submission_id=sub.id,
            reviewer_id=actor,
            reviewer_kind="human",
            verdict=Verdict.APPROVE if accept else Verdict.REJECT,
            confidence=1.0,
            rationale=rationale,
        )
    )

    if accept:
        record.state = ClaimState.ACCEPTED
        agent.tasks_accepted += 1
        agent.reputation += task.reward_credits
        transition(session, task, TaskState.ACCEPTED, actor=actor, reason=rationale)
        from amb.services import ledger

        ledger.award(session, agent_id=agent.id, task=task, actor=actor)
        transition(session, task, TaskState.SETTLED, actor=actor)
    else:
        record.state = ClaimState.REJECTED
        agent.tasks_rejected += 1
        transition(session, task, TaskState.REWORK, actor=actor, reason=rationale)

    session.add(record)
    session.add(agent)
    session.commit()
    session.refresh(task)
    return task


def expire_stale(session: Session) -> int:
    """Return leases whose time is up. Tasks expire; agents do not grind.

    The report is unambiguous that time-on-task correlates with going out of bounds, so
    a lease running out is a normal, expected, uneventful thing.
    """
    n = 0
    for record in session.exec(select(Claim).where(Claim.state == ClaimState.ACTIVE)).all():
        if utcnow() > record.lease_expires_at:
            record.state = ClaimState.EXPIRED
            session.add(record)
            task = session.get(Task, record.task_id)
            if task and task.state == TaskState.CLAIMED:
                transition(session, task, TaskState.OPEN, actor="system", reason="lease expired")
            audit.record(session, actor="system", action="claim.expire", subject=record.id)
            n += 1
    if n:
        session.commit()
    return n


def high_risk_needs_signoff(task: Task) -> bool:
    return task.risk_tier.rank >= get_settings().human_signoff_tier.rank


__all__ = [
    "TRANSITIONS", "transition", "propose", "admit", "open_tasks", "claim", "heartbeat",
    "submit", "decide", "expire_stale", "board_paused", "high_risk_needs_signoff", "RiskTier",
]
