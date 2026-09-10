"""Hash-chained audit log.

Every consequential action appends a row that commits to its predecessor. Editing or
removing history breaks the chain, and `amb audit verify` says exactly where.

This is not tamper-*proof* - anyone with write access to the database can rewrite the
whole chain. It is tamper-*evident*, which is the achievable and useful property: the
board's own account of what it did cannot be quietly revised.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlmodel import Session, select

from amb.models import AuditEvent

GENESIS = "0" * 64


def _digest(seq: int, actor: str, action: str, subject: str, detail: dict, prev: str) -> str:
    payload = json.dumps(
        {
            "seq": seq,
            "actor": actor,
            "action": action,
            "subject": subject,
            "detail": detail,
            "prev": prev,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def head(session: Session) -> AuditEvent | None:
    return session.exec(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)).first()


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    subject: str = "",
    detail: dict[str, Any] | None = None,
) -> AuditEvent:
    detail = detail or {}
    tip = head(session)
    seq = (tip.seq + 1) if tip else 1
    prev = tip.hash if tip else GENESIS
    event = AuditEvent(
        seq=seq,
        actor=actor,
        action=action,
        subject=subject,
        detail=detail,
        prev_hash=prev,
        hash=_digest(seq, actor, action, subject, detail, prev),
    )
    session.add(event)
    session.flush()
    return event


def verify(session: Session) -> tuple[bool, str]:
    """Walk the chain. Returns (ok, human-readable explanation)."""
    events = session.exec(select(AuditEvent).order_by(AuditEvent.seq)).all()
    if not events:
        return True, "empty chain (0 events)"

    prev = GENESIS
    for i, ev in enumerate(events):
        if ev.seq != i + 1:
            return False, f"sequence gap at seq={ev.seq}: expected {i + 1}"
        if ev.prev_hash != prev:
            return False, f"broken link at seq={ev.seq}: prev_hash does not match seq={i}"
        expected = _digest(ev.seq, ev.actor, ev.action, ev.subject, ev.detail, ev.prev_hash)
        if ev.hash != expected:
            return False, f"content altered at seq={ev.seq}: '{ev.action}' does not hash to its digest"
        prev = ev.hash
    return True, f"chain intact ({len(events)} events, head {prev[:12]}…)"
