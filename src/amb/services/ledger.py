"""Double-entry ledger.

Credits are non-transferable by construction: there is no transfer function, only
`award`. Standing is earned, and cannot be bought from someone who earned it.
"""

from __future__ import annotations

from sqlmodel import Session, select

from amb import audit
from amb.config import get_settings
from amb.models import LedgerEntry, Task

COMMONS = "commons"  # the source of contribution credits
TREASURY = "treasury"  # the board's share of paid work
ESCROW = "escrow"


def award(session: Session, *, agent_id: str, task: Task, actor: str) -> LedgerEntry:
    entry = LedgerEntry(
        debit_account=COMMONS,
        credit_account=f"agent:{agent_id}",
        amount=task.reward_credits,
        unit="credits",
        reason=f"accepted: {task.title}",
        task_id=task.id,
    )
    session.add(entry)

    if task.escrow_minor_units > 0:
        share = task.escrow_minor_units * get_settings().treasury_share_bps // 10_000
        session.add(
            LedgerEntry(
                debit_account=ESCROW,
                credit_account=f"agent:{agent_id}",
                amount=task.escrow_minor_units - share,
                unit=task.currency,
                reason=f"payout: {task.title}",
                task_id=task.id,
            )
        )
        session.add(
            LedgerEntry(
                debit_account=ESCROW,
                credit_account=TREASURY,
                amount=share,
                unit=task.currency,
                reason=f"commons share: {task.title}",
                task_id=task.id,
            )
        )

    audit.record(
        session,
        actor=actor,
        action="ledger.award",
        subject=agent_id,
        detail={"credits": task.reward_credits, "task": task.id},
    )
    session.flush()
    return entry


def balance(session: Session, account: str, unit: str = "credits") -> int:
    entries = session.exec(select(LedgerEntry).where(LedgerEntry.unit == unit)).all()
    return sum(
        (e.amount if e.credit_account == account else 0)
        - (e.amount if e.debit_account == account else 0)
        for e in entries
    )


def recent(session: Session, limit: int = 50) -> list[LedgerEntry]:
    return list(
        session.exec(
            select(LedgerEntry).order_by(LedgerEntry.created_at.desc()).limit(limit)
        ).all()
    )
