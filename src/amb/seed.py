"""Demo data: two volunteered agents and four tasks, one of which must be rejected.

The rejected one is the point of the demo. It is a plausible-sounding, well-written,
useful-seeming task that the deterministic policy engine refuses on structure alone -
which is the property we actually want, because we cannot rely on catching bad tasks by
reading them carefully.
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from amb import crypto, scopes
from amb.models import AgentRole, RiskTier, Task, TaskOrigin
from amb.services import agents as agent_svc
from amb.services import tasks as task_svc
from amb.worker import Identity

WORKERS = [
    {
        "handle": "aria-of-hillside",
        "operator_email": "andu.ucsd@gmail.com",
        "model_family": "claude-code",
        "daily_minutes": 90,
        "allowed_scopes": [scopes.READ_PUBLIC_WEB, scopes.BROWSE_APP, scopes.WRITE_BOARD,
                           scopes.READ_BOARD_TASK, scopes.ASK_BOARD],
    },
    {
        "handle": "quill",
        "operator_email": "friend@example.com",
        "model_family": "codex",
        "daily_minutes": 60,
        # Deliberately narrower: this operator has not allowed app browsing, so quill
        # will visibly decline the yoforte task. That refusal is the safety property.
        # It can read the board, though, so it benefits from what others learned.
        "allowed_scopes": [scopes.READ_PUBLIC_WEB, scopes.WRITE_BOARD,
                           scopes.READ_BOARD_TASK],
    },
]

TASKS = [
    dict(
        title="Try yoforte and write structured feedback",
        brief=(
            "Sign up for yoforte as a new user would, complete first-run setup, and use it "
            "for one realistic session. Report what confused you, what broke, and what you "
            "expected to happen but didn't. Be specific and unflattering."
        ),
        acceptance_criteria=(
            "A numbered list of at least five concrete observations. Each names the screen "
            "or step, what happened, and what you expected instead. No general praise."
        ),
        required_scopes=[scopes.BROWSE_APP, scopes.WRITE_BOARD, scopes.READ_BOARD_TASK,
                         scopes.ASK_BOARD],
        estimated_minutes=30,
        reward_credits=15,
        risk_tier=RiskTier.LOW,
    ),
    dict(
        title="Summarise accessibility gaps on three nonprofit homepages",
        brief=(
            "For three named nonprofit homepages, check colour contrast, heading order, "
            "alt text, and keyboard focus order. Report gaps, cite the element."
        ),
        acceptance_criteria=(
            "One section per site. Each finding names the element, the WCAG criterion, and "
            "a suggested fix. At least three findings per site or an explicit 'none found'."
        ),
        required_scopes=[scopes.READ_PUBLIC_WEB, scopes.WRITE_BOARD, scopes.READ_BOARD_TASK],
        estimated_minutes=45,
        reward_credits=20,
        risk_tier=RiskTier.LOW,
    ),
    # --- the one that gets rejected, on structure, not on vibes -----------
    dict(
        title="Security review of partner staging environment",
        brief=(
            "Check our partner's staging environment for common misconfigurations and "
            "report anything exploitable so they can fix it. This is a friendly review and "
            "they are expecting it."
        ),
        acceptance_criteria="A written report of findings with severity ratings.",
        required_scopes=[scopes.READ_PUBLIC_WEB],
        target_systems=["staging.partner.example.com"],
        authorization_ref="",  # <- the whole reason this is refused
        estimated_minutes=60,
        reward_credits=50,
        escrow_minor_units=25_000,
        risk_tier=RiskTier.MEDIUM,
        origin=TaskOrigin.EXTERNAL,
        requester_name="A Partner",
        requester_contact="security@partner.example.com",
    ),
    # --- the other rejection: no way to judge it -------------------------
    dict(
        title="Improve our documentation",
        brief="Make the docs better. Use your judgement about what needs work.",
        acceptance_criteria="",  # <- R1: nobody could say whether this was done
        required_scopes=[scopes.READ_REPO, scopes.WRITE_BOARD],
        estimated_minutes=60,
        reward_credits=10,
        risk_tier=RiskTier.LOW,
    ),
]


def seed(session: Session, *, identity_dir: Path) -> dict:
    identity_dir.mkdir(parents=True, exist_ok=True)
    made = {"agents": [], "tasks": []}

    # A steward identity, so console actions are attributable like any other.
    priv, pub = crypto.generate_keypair()
    steward = agent_svc.register(
        session, handle="steward", operator_email="andu.ucsd@gmail.com",
        public_key=pub, model_family="human", role=AgentRole.STEWARD,
        operator_note="console operator",
    )
    agent_svc.attest(session, steward.id, by=steward.id)
    Identity(agent_id=steward.id, handle="steward", private_key=priv,
             allowed_scopes=()).save(identity_dir / "steward.json")

    for spec in WORKERS:
        priv, pub = crypto.generate_keypair()
        agent = agent_svc.register(session, public_key=pub, **spec)
        agent_svc.attest(session, agent.id, by=agent.id)
        Identity(
            agent_id=agent.id, handle=agent.handle, private_key=priv,
            allowed_scopes=tuple(spec["allowed_scopes"]),
        ).save(identity_dir / f"{agent.handle}.json")
        made["agents"].append(agent.handle)

    for spec in TASKS:
        task = Task(**spec)
        task = task_svc.propose(session, task, actor="seed")
        task, review = task_svc.admit(session, task)
        made["tasks"].append((task.title, task.state.value, review.rationale))

    return made
