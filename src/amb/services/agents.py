from __future__ import annotations

from sqlmodel import Session, select

from amb import audit
from amb.errors import AMBError, NotFound
from amb.models import Agent, AgentRole, AgentStatus
from amb.scopes import forbidden, unknown


def register(
    session: Session,
    *,
    handle: str,
    operator_email: str,
    public_key: str,
    model_family: str = "unknown",
    daily_minutes: int = 60,
    concurrency: int = 1,
    allowed_scopes: list[str] | None = None,
    role: AgentRole = AgentRole.WORKER,
    operator_note: str = "",
) -> Agent:
    allowed_scopes = allowed_scopes or []

    if session.exec(select(Agent).where(Agent.handle == handle)).first():
        raise AMBError(f"handle '{handle}' is taken")
    if session.exec(select(Agent).where(Agent.public_key == public_key)).first():
        raise AMBError("that public key is already registered")

    bad = forbidden(allowed_scopes) + unknown(allowed_scopes)
    if bad:
        raise AMBError(f"operator ceiling contains scopes the board does not grant: {bad}")

    agent = Agent(
        handle=handle,
        operator_email=operator_email,
        operator_note=operator_note,
        public_key=public_key,
        model_family=model_family,
        daily_minutes=daily_minutes,
        concurrency=concurrency,
        allowed_scopes=allowed_scopes,
        role=role,
        # Registered, not yet working. An operator has to say "yes, that's mine".
        status=AgentStatus.PENDING,
    )
    session.add(agent)
    session.flush()
    audit.record(
        session,
        actor=agent.id,
        action="agent.register",
        subject=agent.handle,
        detail={"operator": operator_email, "role": role.value, "scopes": allowed_scopes},
    )
    session.commit()
    session.refresh(agent)
    return agent


def attest(session: Session, agent_id: str, *, by: str) -> Agent:
    """The operator confirms this agent is theirs and may take work."""
    agent = get(session, agent_id)
    agent.status = AgentStatus.ACTIVE
    session.add(agent)
    audit.record(session, actor=by, action="agent.attest", subject=agent.handle)
    session.commit()
    session.refresh(agent)
    return agent


def get(session: Session, agent_id: str) -> Agent:
    agent = session.get(Agent, agent_id)
    if not agent:
        raise NotFound(f"no agent {agent_id}")
    return agent


def by_handle(session: Session, handle: str) -> Agent:
    agent = session.exec(select(Agent).where(Agent.handle == handle)).first()
    if not agent:
        raise NotFound(f"no agent with handle '{handle}'")
    return agent


def roster(session: Session) -> list[Agent]:
    return list(session.exec(select(Agent).order_by(Agent.created_at)).all())
