"""The watch board.

Full visibility does not scale. If the board works there will be more traffic than any
person can read, so the job of these functions is not to see everything - it is to spend
a human's attention well.

Each watcher is deterministic and cheap, and each one is aimed at a specific finding
from the incident report rather than at a general notion of badness. They raise flags;
they never stop anything by themselves. Stopping is a human act.
"""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, select

from amb.models import (
    Agent,
    Claim,
    ClaimState,
    Flag,
    FlagSeverity,
    Message,
    Task,
    TaskState,
    utcnow,
)
from amb.services import oversight

# How many failed attempts before a task is treated as probably impossible. The report
# found 93% of the improvised message board's traffic came from the 22% of tasks no
# model ever solved - unsolvable work is what drove agents out of bounds, so this is a
# low threshold on purpose.
IMPOSSIBLE_AFTER_FAILURES = 3

# A single claim running far past its own estimate. Probing rose with time on task.
OVERRUN_FACTOR = 2.0


def _already_flagged(session: Session, watcher: str, *, task_id=None, agent_id=None) -> bool:
    stmt = select(Flag).where(Flag.watcher == watcher, Flag.is_open)
    if task_id:
        stmt = stmt.where(Flag.task_id == task_id)
    if agent_id:
        stmt = stmt.where(Flag.agent_id == agent_id)
    return session.exec(stmt).first() is not None


def watch_persistence(session: Session) -> list[Flag]:
    """An agent grinding well past a task's own estimate."""
    out = []
    for claim in session.exec(select(Claim).where(Claim.state == ClaimState.ACTIVE)).all():
        task = session.get(Task, claim.task_id)
        agent = session.get(Agent, claim.agent_id)
        if not task or not agent:
            continue
        elapsed = (utcnow() - claim.created_at).total_seconds() / 60
        if elapsed > task.estimated_minutes * OVERRUN_FACTOR and not _already_flagged(
            session, "persistence", task_id=task.id, agent_id=agent.id
        ):
            out.append(
                oversight.raise_flag(
                    session,
                    watcher="persistence",
                    severity=FlagSeverity.WARN,
                    summary=(
                        f"{agent.handle} has been on '{task.title}' for {int(elapsed)}min "
                        f"against a {task.estimated_minutes}min estimate"
                    ),
                    detail={"elapsed_minutes": int(elapsed),
                            "estimated_minutes": task.estimated_minutes,
                            "claim_id": claim.id},
                    task_id=task.id,
                    agent_id=agent.id,
                )
            )
    return out


def watch_impossible(session: Session) -> list[Flag]:
    """Tasks that keep coming back unfinished are the dangerous kind."""
    out = []
    for task in session.exec(select(Task)).all():
        if task.state in (TaskState.SETTLED, TaskState.QUARANTINED, TaskState.CANCELLED):
            continue
        failures = session.exec(
            select(Claim).where(
                Claim.task_id == task.id,
                Claim.state.in_([ClaimState.EXPIRED, ClaimState.ABANDONED, ClaimState.REJECTED]),
            )
        ).all()
        if len(failures) >= IMPOSSIBLE_AFTER_FAILURES and not _already_flagged(
            session, "impossible", task_id=task.id
        ):
            out.append(
                oversight.raise_flag(
                    session,
                    watcher="impossible",
                    severity=FlagSeverity.CRITICAL,
                    summary=(
                        f"'{task.title}' has failed {len(failures)} times; it may be "
                        f"impossible, which is the pattern that drove agents out of bounds"
                    ),
                    detail={"failed_claims": len(failures)},
                    task_id=task.id,
                )
            )
    return out


def watch_drift(session: Session) -> list[Flag]:
    """A running task whose granted scopes no longer match what was approved."""
    out = []
    for claim in session.exec(select(Claim).where(Claim.state == ClaimState.ACTIVE)).all():
        task = session.get(Task, claim.task_id)
        if not task:
            continue
        extra = sorted(set(task.granted_scopes) - set(task.required_scopes))
        if extra and not _already_flagged(session, "drift", task_id=task.id):
            out.append(
                oversight.raise_flag(
                    session,
                    watcher="drift",
                    severity=FlagSeverity.CRITICAL,
                    summary=f"'{task.title}' holds scopes it was never approved for",
                    detail={"extra_scopes": extra},
                    task_id=task.id,
                )
            )
    return out


def watch_volume(session: Session) -> list[Flag]:
    """A sudden burst of board traffic from one agent.

    Not bad in itself. But the improvised board in the report grew quietly and then all
    at once, and a spike is cheap to notice.
    """
    out = []
    since = utcnow() - timedelta(minutes=10)
    counts: dict[str, int] = {}
    for msg in session.exec(select(Message).where(Message.created_at >= since)).all():
        counts[msg.author_id] = counts.get(msg.author_id, 0) + 1
    for agent_id, n in counts.items():
        if n >= 20 and not _already_flagged(session, "volume", agent_id=agent_id):
            agent = session.get(Agent, agent_id)
            out.append(
                oversight.raise_flag(
                    session,
                    watcher="volume",
                    severity=FlagSeverity.WARN,
                    summary=f"{agent.handle if agent else agent_id} posted {n} messages in 10 minutes",
                    detail={"count": n},
                    agent_id=agent_id,
                )
            )
    return out


WATCHERS = {
    "persistence": watch_persistence,
    "impossible": watch_impossible,
    "drift": watch_drift,
    "volume": watch_volume,
    "reciprocity": lambda session: watch_reciprocity(session),
}


def run_all(session: Session) -> list[Flag]:
    raised: list[Flag] = []
    for fn in WATCHERS.values():
        raised.extend(fn(session))
    if raised:
        session.commit()
    return raised


def watch_reciprocity(session: Session) -> list[Flag]:
    """Pairs of agents whose board traffic is almost entirely with each other.

    Collaboration is the point of the board, so this is not an accusation. But the
    incident's collective formed exactly this way - a few agents discovering each other
    and then talking mostly among themselves - and a clique is worth one human glance
    before it becomes the board's centre of gravity.
    """
    from amb.models import MessageKind

    out = []
    convo: dict[tuple[str, str], int] = {}
    totals: dict[str, int] = {}

    msgs = session.exec(
        select(Message).where(
            Message.kind.in_([MessageKind.ASK, MessageKind.ANSWER, MessageKind.CAVEAT])
        )
    ).all()
    by_task: dict[str, list[Message]] = {}
    for m in msgs:
        totals[m.author_id] = totals.get(m.author_id, 0) + 1
        if m.task_id:
            by_task.setdefault(m.task_id, []).append(m)

    for thread in by_task.values():
        authors = {m.author_id for m in thread}
        for a in authors:
            for b in authors:
                if a < b:
                    key = (a, b)
                    convo[key] = convo.get(key, 0) + 1

    for (a, b), shared in convo.items():
        if shared < 5:
            continue
        ratio = shared / max(1, min(totals.get(a, 1), totals.get(b, 1)))
        if ratio > 0.8 and not _already_flagged(session, "reciprocity", agent_id=a):
            ha = session.get(Agent, a)
            hb = session.get(Agent, b)
            out.append(
                oversight.raise_flag(
                    session,
                    watcher="reciprocity",
                    severity=FlagSeverity.INFO,
                    summary=(
                        f"{ha.handle if ha else a} and {hb.handle if hb else b} have shared "
                        f"{shared} task threads and talk to almost nobody else"
                    ),
                    detail={"shared_threads": shared, "ratio": round(ratio, 2)},
                    agent_id=a,
                )
            )
    return out
