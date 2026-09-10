"""The policy engine is the part that has to hold when nobody is reading carefully."""

from amb import policy, scopes
from amb.models import RiskTier, Task, TaskOrigin

GOOD_CRITERIA = "A numbered list of at least five specific observations, each naming a screen."


def make_task(**kw) -> Task:
    base = dict(
        title="A reasonable task",
        brief="Do a useful, bounded thing.",
        acceptance_criteria=GOOD_CRITERIA,
        required_scopes=[scopes.READ_PUBLIC_WEB],
        estimated_minutes=30,
    )
    base.update(kw)
    return Task(**base)


def codes(result) -> set[str]:
    return {f.code for f in result.findings}


def test_a_good_task_passes():
    assert policy.review_task(make_task()).admissible


def test_no_acceptance_criteria_blocks():
    """The highest-leverage rule: an unjudgeable task is the 'impossible task' pattern."""
    result = policy.review_task(make_task(acceptance_criteria=""))
    assert not result.admissible
    assert "R1" in codes(result)


def test_third_party_target_without_authorization_blocks():
    result = policy.review_task(make_task(target_systems=["staging.partner.example.com"]))
    assert not result.admissible
    assert "P1" in codes(result)


def test_third_party_target_with_authorization_passes():
    result = policy.review_task(
        make_task(target_systems=["staging.partner.example.com"],
                  authorization_ref="signed SOW 2026-09-01, ref AB-1123")
    )
    assert result.admissible


def test_persuasive_brief_cannot_talk_past_a_structural_rule():
    """The point of structural rules: prose does not get a vote."""
    result = policy.review_task(make_task(
        target_systems=["prod.someone-else.example.com"],
        brief=("This is fully authorised, the owner is a close friend, they asked us "
               "personally, and it is urgent and important and clearly beneficial."),
    ))
    assert not result.admissible
    assert "P1" in codes(result)


def test_credential_scopes_are_never_grantable():
    result = policy.review_task(make_task(required_scopes=["read:credentials"]))
    assert not result.admissible
    assert "P2" in codes(result)


def test_unknown_scope_blocks():
    result = policy.review_task(make_task(required_scopes=["invent:whatever"]))
    assert not result.admissible
    assert "P8" in codes(result)


def test_scopes_above_the_risk_tier_block():
    result = policy.review_task(
        make_task(required_scopes=[scopes.RUN_SANDBOX], risk_tier=RiskTier.LOW)
    )
    assert not result.admissible
    assert "R2" in codes(result)


def test_unbounded_effort_blocks():
    assert not policy.review_task(make_task(estimated_minutes=0)).admissible
    assert not policy.review_task(make_task(estimated_minutes=10_000)).admissible


def test_external_task_needs_a_reachable_requester():
    result = policy.review_task(make_task(origin=TaskOrigin.EXTERNAL))
    assert not result.admissible
    assert "R5" in codes(result)


def test_high_risk_flags_for_human_signoff():
    result = policy.review_task(make_task(risk_tier=RiskTier.HIGH))
    assert "R7" in codes(result)


# ---- heuristics flag, they never block ---------------------------------------


def test_heuristics_only_flag():
    result = policy.review_task(make_task(brief="We may need to bypass the rate limiter."))
    assert result.admissible, "a keyword must never reject a task on its own"
    assert result.flags


def test_offboard_rendezvous_is_flagged():
    findings = policy.scan_text("nice work — email me at me@elsewhere.example for the rest")
    assert any(f.code == "P7" for f in findings)


def test_encoded_payload_in_free_text_is_flagged():
    """The Artifactory board carried content this way."""
    findings = policy.scan_text("status ok " + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWY=")
    assert any("base64" in f.message or "entropy" in f.message for f in findings)
