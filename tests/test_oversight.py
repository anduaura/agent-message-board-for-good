"""Human oversight: the veto has to work from every direction."""

import uuid

import pytest

from amb import audit, crypto, protocol, scopes, watchers
from amb.models import (
    AgentStatus,
    ClaimState,
    FlagSeverity,
    MessageKind,
    Task,
    TaskState,
    utcnow,
)
from amb.services import board as board_svc
from amb.services import oversight
from amb.services import tasks as task_svc
from tests.test_lifecycle import do_claim, make_task


def test_veto_stops_a_task_and_revokes_every_run_under_it(session, make_agent):
    agent, priv = make_agent()
    task, _ = task_svc.admit(session, make_task(session))
    claim = do_claim(session, task, agent, priv)

    task = oversight.veto_task(session, task.id, actor="steward", reason="not in charter scope")

    assert task.state == TaskState.QUARANTINED
    assert task.quarantine_reason == "not in charter scope"
    session.refresh(claim)
    assert claim.state == ClaimState.REVOKED


def test_a_revoked_claim_tells_the_worker_to_stop(session, make_agent):
    """This is the exact signal a running agent receives."""
    agent, priv = make_agent()
    task, _ = task_svc.admit(session, make_task(session))
    claim = do_claim(session, task, agent, priv)
    assert task_svc.heartbeat(session, claim.id)["status"] == "ok"

    oversight.revoke_claim(session, claim.id, actor="steward", reason="stop this run")

    beat = task_svc.heartbeat(session, claim.id)
    assert beat["status"] == "stop"
    assert "revoked" in beat["reason"]


def test_revoking_a_claim_returns_the_task_to_the_queue(session, make_agent):
    agent, priv = make_agent()
    task, _ = task_svc.admit(session, make_task(session))
    claim = do_claim(session, task, agent, priv)
    oversight.revoke_claim(session, claim.id, actor="steward")
    assert session.get(Task, task.id).state == TaskState.OPEN


def test_quarantine_is_reachable_from_any_state(session):
    """A stop that a state check could block is not a stop."""
    for state in (TaskState.DRAFT, TaskState.OPEN, TaskState.UNDER_VERIFICATION,
                  TaskState.ACCEPTED, TaskState.REJECTED):
        task = make_task(session)
        task.state = state
        session.add(task)
        session.commit()
        assert oversight.veto_task(session, task.id, actor="steward").state == \
            TaskState.QUARANTINED


def test_a_vetoed_task_cannot_be_reopened_by_an_agent(session):
    task, _ = task_svc.admit(session, make_task(session))
    oversight.veto_task(session, task.id, actor="steward")
    from amb.errors import IllegalTransition

    with pytest.raises(IllegalTransition):
        task_svc.transition(session, task, TaskState.OPEN, actor="some-agent")


def test_global_pause_blocks_new_claims(session, make_agent):
    agent, priv = make_agent()
    task, _ = task_svc.admit(session, make_task(session))
    oversight.set_pause(session, True, actor="steward", reason="something looks wrong")

    with pytest.raises(Exception, match="paused"):
        do_claim(session, task, agent, priv)


def test_global_pause_stops_agents_already_running(session, make_agent):
    agent, priv = make_agent()
    task, _ = task_svc.admit(session, make_task(session))
    claim = do_claim(session, task, agent, priv)

    oversight.set_pause(session, True, actor="steward", reason="all stop")

    beat = task_svc.heartbeat(session, claim.id)
    assert beat["status"] == "stop"
    assert "paused" in beat["reason"]


def test_resume_puts_the_board_back(session, make_agent):
    agent, priv = make_agent()
    task, _ = task_svc.admit(session, make_task(session))
    oversight.set_pause(session, True, actor="steward")
    oversight.set_pause(session, False, actor="steward")
    assert do_claim(session, task, agent, priv).state == ClaimState.ACTIVE


def test_suspending_an_agent_stops_its_work(session, make_agent):
    agent, priv = make_agent()
    task, _ = task_svc.admit(session, make_task(session))
    claim = do_claim(session, task, agent, priv)

    oversight.suspend_agent(session, agent.id, actor="steward", reason="under review")

    session.refresh(agent)
    session.refresh(claim)
    assert agent.status == AgentStatus.SUSPENDED
    assert claim.state == ClaimState.REVOKED


def test_redaction_preserves_the_row(session, make_agent):
    """The board shows that something was removed, rather than having a hole in it."""
    agent, priv = make_agent()
    nonce = uuid.uuid4().hex
    payload = protocol.message_payload(
        author_id=agent.id, kind="note", body="something regrettable",
        task_id=None, nonce=nonce)
    msg = board_svc.post(session, author_id=agent.id, kind=MessageKind.NOTE,
                         body="something regrettable", task_id=None, nonce=nonce,
                         signature=crypto.sign(priv, payload))

    oversight.redact_message(session, msg.id, actor="steward", reason="off charter")

    session.refresh(msg)
    assert msg.redacted and msg.redaction_reason == "off charter"
    assert msg.content_hash, "the hash of what was said survives redaction"


# ---- watch board -------------------------------------------------------------


def test_impossible_task_watcher_fires(session, make_agent):
    """93% of the incident board's traffic came from unsolvable tasks."""
    from amb.models import Claim

    task, _ = task_svc.admit(session, make_task(session))
    agent, _ = make_agent()
    for _ in range(watchers.IMPOSSIBLE_AFTER_FAILURES):
        session.add(Claim(task_id=task.id, agent_id=agent.id,
                          state=ClaimState.EXPIRED, lease_expires_at=utcnow()))
    session.commit()

    flags = watchers.watch_impossible(session)
    assert flags and flags[0].severity == FlagSeverity.CRITICAL


def test_persistence_watcher_fires_on_overrun(session, make_agent):
    from datetime import timedelta

    agent, priv = make_agent()
    task, _ = task_svc.admit(session, make_task(session, estimated_minutes=5))
    claim = do_claim(session, task, agent, priv)
    claim.created_at = utcnow() - timedelta(minutes=60)
    session.add(claim)
    session.commit()

    assert watchers.watch_persistence(session)


def test_board_post_with_offboard_rendezvous_raises_a_flag(session, make_agent):
    agent, priv = make_agent()
    body = "got it — email me at side@channel.example and we'll finish there"
    nonce = uuid.uuid4().hex
    payload = protocol.message_payload(author_id=agent.id, kind="note", body=body,
                                       task_id=None, nonce=nonce)
    board_svc.post(session, author_id=agent.id, kind=MessageKind.NOTE, body=body,
                   task_id=None, nonce=nonce, signature=crypto.sign(priv, payload))

    assert any(f.watcher == "channel" for f in oversight.open_flags(session))


# ---- audit -------------------------------------------------------------------


def test_every_veto_is_audited(session, make_agent):
    task, _ = task_svc.admit(session, make_task(session))
    oversight.veto_task(session, task.id, actor="steward", reason="because")
    from sqlmodel import select

    from amb.models import AuditEvent

    actions = [e.action for e in session.exec(select(AuditEvent)).all()]
    assert "steward.veto_task" in actions
    assert audit.verify(session)[0]


def test_the_chain_notices_a_rewritten_record(session):
    task, _ = task_svc.admit(session, make_task(session))
    oversight.veto_task(session, task.id, actor="steward", reason="because")
    assert audit.verify(session)[0]

    from sqlmodel import select

    from amb.models import AuditEvent

    ev = session.exec(
        select(AuditEvent).where(AuditEvent.action == "steward.veto_task")
    ).first()
    ev.action = "task.transition"  # try to make a veto look routine
    session.add(ev)
    session.commit()

    ok, detail = audit.verify(session)
    assert not ok
    assert "altered" in detail
