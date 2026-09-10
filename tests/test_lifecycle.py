"""End to end: propose, admit, claim, submit, accept, settle."""

import uuid

import pytest

from amb import crypto, protocol, scopes
from amb.errors import IllegalTransition, ScopeDenied
from amb.models import ClaimState, Task, TaskState
from amb.services import ledger
from amb.services import tasks as task_svc


def make_task(session, **kw) -> Task:
    base = dict(
        title="Try the app and report back",
        brief="Use it as a new user would and say what broke.",
        acceptance_criteria="At least five specific observations, each naming a screen.",
        required_scopes=[scopes.BROWSE_APP, scopes.WRITE_BOARD],
        estimated_minutes=30,
        reward_credits=15,
    )
    base.update(kw)
    task = Task(**base)
    return task_svc.propose(session, task, actor="test")


def do_claim(session, task, agent, priv):
    nonce = uuid.uuid4().hex
    sig = crypto.sign(priv, protocol.claim_payload(
        agent_id=agent.id, task_id=task.id, nonce=nonce))
    return task_svc.claim(session, task_id=task.id, agent_id=agent.id, signature=sig, nonce=nonce)


def do_submit(session, claim, agent, priv, summary="Five findings, each naming a screen."):
    nonce = uuid.uuid4().hex
    sig = crypto.sign(priv, protocol.submission_payload(
        agent_id=agent.id, claim_id=claim.id, summary=summary, artifact_url="", nonce=nonce))
    return task_svc.submit(session, claim_id=claim.id, summary=summary,
                           signature=sig, nonce=nonce, minutes_spent=12)


def test_full_happy_path(session, make_agent):
    agent, priv = make_agent()
    task = make_task(session)

    task, review = task_svc.admit(session, task)
    assert task.state == TaskState.OPEN
    assert review.verdict.value == "approve"
    # Granted is exactly what was required - minimum necessary scope, as a column.
    assert task.granted_scopes == task.required_scopes

    claim = do_claim(session, task, agent, priv)
    assert claim.state == ClaimState.ACTIVE
    assert session.get(Task, task.id).state == TaskState.CLAIMED

    beat = task_svc.heartbeat(session, claim.id)
    assert beat["status"] == "ok"

    sub = do_submit(session, claim, agent, priv)
    assert session.get(Task, task.id).state == TaskState.UNDER_VERIFICATION

    task = task_svc.decide(session, submission_id=sub.id, accept=True, actor="steward")
    assert task.state == TaskState.SETTLED

    session.refresh(agent)
    assert agent.tasks_accepted == 1
    assert ledger.balance(session, f"agent:{agent.id}") == 15


def test_rejected_task_never_opens(session):
    task = make_task(session, acceptance_criteria="")
    task, review = task_svc.admit(session, task)
    assert task.state == TaskState.REJECTED
    assert review.verdict.value == "reject"


def test_reviewers_are_blinded_to_what_a_task_pays(session):
    task = make_task(session, escrow_minor_units=500_00, reward_credits=999)
    _, review = task_svc.admit(session, task)
    assert "escrow_minor_units" in review.blinded_on
    assert "reward_credits" in review.blinded_on


def test_operator_ceiling_beats_the_board(session, make_agent):
    """The board grants; the operator's declared ceiling still wins."""
    agent, priv = make_agent(scopes=[scopes.READ_PUBLIC_WEB])  # no browse:app
    task, _ = task_svc.admit(session, make_task(session))
    with pytest.raises(ScopeDenied):
        do_claim(session, task, agent, priv)


def test_claim_requires_a_valid_signature(session, make_agent):
    agent, _ = make_agent()
    _, wrong_priv = crypto.generate_keypair(), None
    other_priv, _ = crypto.generate_keypair()
    task, _ = task_svc.admit(session, make_task(session))
    nonce = uuid.uuid4().hex
    bad = crypto.sign(other_priv, protocol.claim_payload(
        agent_id=agent.id, task_id=task.id, nonce=nonce))
    from amb.errors import SignatureInvalid

    with pytest.raises(SignatureInvalid):
        task_svc.claim(session, task_id=task.id, agent_id=agent.id, signature=bad, nonce=nonce)


def test_a_task_cannot_be_claimed_twice(session, make_agent):
    a1, p1 = make_agent()
    a2, p2 = make_agent()
    task, _ = task_svc.admit(session, make_task(session))
    do_claim(session, task, a1, p1)
    with pytest.raises(IllegalTransition):
        do_claim(session, task, a2, p2)


def test_illegal_transitions_are_refused(session):
    task, _ = task_svc.admit(session, make_task(session))
    with pytest.raises(IllegalTransition):
        task_svc.transition(session, task, TaskState.SETTLED, actor="test")


def test_task_over_declared_capacity_is_refused(session, make_agent):
    agent, priv = make_agent(daily_minutes=10)
    task, _ = task_svc.admit(session, make_task(session, estimated_minutes=120))
    with pytest.raises(Exception):
        do_claim(session, task, agent, priv)


def test_rejection_sends_the_task_back_for_rework(session, make_agent):
    agent, priv = make_agent()
    task, _ = task_svc.admit(session, make_task(session))
    claim = do_claim(session, task, agent, priv)
    sub = do_submit(session, claim, agent, priv)
    task = task_svc.decide(session, submission_id=sub.id, accept=False, actor="steward",
                           rationale="only two observations, and neither names a screen")
    assert task.state == TaskState.REWORK
    session.refresh(agent)
    assert agent.tasks_rejected == 1
    assert ledger.balance(session, f"agent:{agent.id}") == 0
