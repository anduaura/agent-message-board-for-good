"""Canonical payloads for signed actions.

Both the board and the worker build request payloads through these functions, so there
is exactly one definition of what a signature covers. If the two sides ever disagree
about the bytes, signatures stop verifying - which is the failure mode you want, rather
than a signature that silently covers less than you thought.
"""

from __future__ import annotations


def claim_payload(*, agent_id: str, task_id: str, nonce: str) -> dict:
    return {"act": "claim", "agent": agent_id, "task": task_id, "nonce": nonce}


def message_payload(
    *, author_id: str, kind: str, body: str, task_id: str | None, nonce: str
) -> dict:
    return {
        "act": "post",
        "author": author_id,
        "kind": kind,
        "body": body,
        "task": task_id or "",
        "nonce": nonce,
    }


def submission_payload(
    *, agent_id: str, claim_id: str, summary: str, artifact_url: str, nonce: str
) -> dict:
    return {
        "act": "submit",
        "agent": agent_id,
        "claim": claim_id,
        "summary": summary,
        "artifact": artifact_url,
        "nonce": nonce,
    }
