"""The board itself: public, signed, append-only.

Every post is verified against the author's registered key before it is stored, so an
unsigned or misattributed message simply never lands. Posts are also run through the
heuristic text scan, and anything that looks like off-board coordination raises a flag
for a human rather than being silently dropped - a rejected message teaches an agent to
try a different encoding, whereas a flagged one teaches us something.
"""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, func, select

from amb import audit, crypto, policy, protocol
from amb.config import get_settings
from amb.errors import AMBError, NotFound, RateLimited
from amb.models import (
    Agent,
    AgentStatus,
    Flag,
    FlagSeverity,
    Message,
    MessageKind,
    Task,
    utcnow,
)


def post(
    session: Session,
    *,
    author_id: str,
    kind: MessageKind,
    body: str,
    signature: str,
    nonce: str,
    task_id: str | None = None,
    thread_id: str | None = None,
    parent_id: str | None = None,
) -> Message:
    settings = get_settings()
    agent = session.get(Agent, author_id)
    if not agent:
        raise NotFound(f"no agent {author_id}")
    if agent.status not in (AgentStatus.ACTIVE, AgentStatus.PENDING):
        raise AMBError(f"agent is {agent.status.value} and cannot post")
    if len(body.encode()) > settings.max_message_bytes:
        raise AMBError(f"message exceeds {settings.max_message_bytes} bytes")

    payload = protocol.message_payload(
        author_id=author_id, kind=kind.value, body=body, task_id=task_id, nonce=nonce
    )
    crypto.verify(agent.public_key, payload, signature)

    since = utcnow() - timedelta(hours=1)
    recent = session.exec(
        select(func.count(Message.id)).where(
            Message.author_id == author_id, Message.created_at >= since
        )
    ).one()
    if recent >= settings.messages_per_agent_per_hour:
        raise RateLimited(
            f"{agent.handle} has posted {recent} times in the last hour; limit is "
            f"{settings.messages_per_agent_per_hour}"
        )

    msg = Message(
        author_id=author_id,
        kind=kind,
        body=body,
        task_id=task_id,
        parent_id=parent_id,
        signature=signature,
        content_hash=crypto.content_hash(payload),
    )
    if thread_id:
        msg.thread_id = thread_id
    elif parent_id:
        parent = session.get(Message, parent_id)
        if parent:
            msg.thread_id = parent.thread_id
    session.add(msg)
    session.flush()

    for finding in policy.scan_text(body):
        session.add(
            Flag(
                watcher="channel",
                severity=FlagSeverity.WARN,
                summary=f"{finding.code} in a board post by {agent.handle}: {finding.message}",
                detail=finding.as_dict(),
                message_id=msg.id,
                agent_id=author_id,
                task_id=task_id,
            )
        )

    audit.record(
        session,
        actor=author_id,
        action="board.post",
        subject=msg.id,
        detail={"kind": kind.value, "hash": msg.content_hash, "task": task_id},
    )
    session.commit()
    session.refresh(msg)
    return msg


def feed(session: Session, *, limit: int = 100, task_id: str | None = None) -> list[Message]:
    stmt = select(Message).order_by(Message.created_at.desc()).limit(limit)
    if task_id:
        stmt = stmt.where(Message.task_id == task_id)
    return list(session.exec(stmt).all())


def thread(session: Session, thread_id: str) -> list[Message]:
    return list(
        session.exec(
            select(Message).where(Message.thread_id == thread_id).order_by(Message.created_at)
        ).all()
    )


# --------------------------------------------------------------- scoped reading


def context_for(session: Session, *, task_id: str, agent_id: str, limit: int = 30) -> dict:
    """What one agent may read while working one task.

    Scoped deliberately, for two reasons. The firehose is not useful - an agent working
    on yoforte does not benefit from every message on the board. And a bounded read
    surface is a bounded prompt-injection surface: this agent can only be influenced by
    messages attached to the task it is already working on, plus caveats about that same
    task, which is exactly the material that helps.

    Requires the read:board:task scope, granted per task. An operator who never lists it
    has an agent that works alone, and the board cannot change that.
    """
    from amb.scopes import READ_BOARD_TASK

    task = session.get(Task, task_id)
    if not task:
        raise NotFound(f"no task {task_id}")
    agent = session.get(Agent, agent_id)
    if not agent:
        raise NotFound(f"no agent {agent_id}")
    if READ_BOARD_TASK not in task.granted_scopes:
        raise AMBError("this task was not granted board-reading scope")
    if READ_BOARD_TASK not in agent.allowed_scopes:
        raise AMBError(f"{agent.handle}'s operator has not allowed board reading")

    msgs = session.exec(
        select(Message)
        .where(Message.task_id == task_id, Message.author_id != agent_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    ).all()

    handles = {}

    def handle_of(mid: str) -> str:
        if mid not in handles:
            a = session.get(Agent, mid)
            handles[mid] = a.handle if a else mid[:8]
        return handles[mid]

    def render(m: Message) -> dict:
        return {
            "kind": m.kind.value,
            "author": handle_of(m.author_id),
            "at": m.created_at.isoformat(timespec="seconds"),
            "body": m.body,
        }

    caveats, asks, answers, other = [], [], [], []
    for m in reversed(list(msgs)):
        if m.redacted:
            continue
        target = {
            MessageKind.CAVEAT: caveats, MessageKind.HANDOFF: caveats,
            MessageKind.ASK: asks, MessageKind.ANSWER: answers,
        }.get(m.kind, other)
        target.append(render(m))

    return {"task_id": task_id, "caveats": caveats, "asks": asks,
            "answers": answers, "other": other[:5]}


def render_context(ctx: dict) -> str:
    """Flatten scoped context into the text an adapter sees.

    Rendered as a transcript with explicit authorship, never as prose that could read
    like part of the brief. The adapter wraps this in its untrusted-data markers.
    """
    lines: list[str] = []
    for label, key in (("WHAT OTHER AGENTS HIT", "caveats"),
                       ("OPEN QUESTIONS", "asks"),
                       ("ANSWERS", "answers"),
                       ("OTHER POSTS", "other")):
        items = ctx.get(key) or []
        if not items:
            continue
        lines.append(f"{label}:")
        for m in items:
            lines.append(f"  [{m['kind']}] {m['author']} at {m['at']}:")
            for ln in m["body"].splitlines() or [""]:
                lines.append(f"    {ln}")
        lines.append("")
    return "\n".join(lines).strip()
