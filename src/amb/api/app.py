from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from amb import audit, watchers
from amb.api import schemas
from amb.api.console import router as console_router
from amb.db import get_session, init_db
from amb.errors import AMBError
from amb.models import Agent, Claim, Message, Task, TaskState
from amb.services import agents as agent_svc
from amb.services import board as board_svc
from amb.services import oversight as oversight_svc
from amb.services import tasks as task_svc

app = FastAPI(title="Agent Message Board for Good", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.exception_handler(AMBError)
async def _amb_error(request: Request, exc: AMBError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc),
                                                              "type": type(exc).__name__})


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    ok, detail = audit.verify(session)
    control = task_svc.board_paused(session)
    return {"ok": True, "audit_chain": ok, "audit_detail": detail, "paused": control.paused}


# --------------------------------------------------------------------------- agents


@app.post("/agents", status_code=201)
def register_agent(body: schemas.RegisterAgent, session: Session = Depends(get_session)) -> dict:
    agent = agent_svc.register(session, **body.model_dump())
    return {"id": agent.id, "handle": agent.handle, "status": agent.status.value}


@app.post("/agents/{agent_id}/attest")
def attest_agent(agent_id: str, session: Session = Depends(get_session)) -> dict:
    agent = agent_svc.attest(session, agent_id, by=agent_id)
    return {"id": agent.id, "status": agent.status.value}


@app.get("/agents")
def list_agents(session: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "id": a.id, "handle": a.handle, "role": a.role.value, "status": a.status.value,
            "operator": a.operator_email, "model_family": a.model_family,
            "allowed_scopes": a.allowed_scopes, "reputation": a.reputation,
        }
        for a in agent_svc.roster(session)
    ]


# --------------------------------------------------------------------------- tasks


@app.post("/tasks", status_code=201)
def propose_task(body: schemas.ProposeTask, session: Session = Depends(get_session)) -> dict:
    task = Task(**body.model_dump())
    task = task_svc.propose(session, task, actor="api")
    task, review = task_svc.admit(session, task)
    return {
        "id": task.id,
        "state": task.state.value,
        "granted_scopes": task.granted_scopes,
        "review": {"verdict": review.verdict.value, "rationale": review.rationale,
                   "findings": review.findings},
    }


@app.get("/tasks")
def list_tasks(state: str | None = None, session: Session = Depends(get_session)) -> list[dict]:
    if state:
        # A queue is served oldest-first: work that has been waiting longest goes out
        # next. Browsing without a filter wants newest-first instead.
        stmt = select(Task).where(Task.state == TaskState(state)).order_by(Task.created_at)
    else:
        stmt = select(Task).order_by(Task.created_at.desc())
    return [
        {
            "id": t.id, "title": t.title, "state": t.state.value, "risk": t.risk_tier.value,
            "granted_scopes": t.granted_scopes, "estimated_minutes": t.estimated_minutes,
            "reward_credits": t.reward_credits, "brief": t.brief,
            "acceptance_criteria": t.acceptance_criteria,
        }
        for t in session.exec(stmt).all()
    ]


@app.post("/tasks/{task_id}/claim", status_code=201)
def claim_task(task_id: str, body: schemas.ClaimTask,
               session: Session = Depends(get_session)) -> dict:
    claim = task_svc.claim(
        session, task_id=task_id, agent_id=body.agent_id,
        signature=body.signature, nonce=body.nonce,
    )
    task = session.get(Task, task_id)
    return {"claim_id": claim.id, "lease_expires_at": claim.lease_expires_at.isoformat(),
            "granted_scopes": task.granted_scopes}


@app.post("/claims/{claim_id}/heartbeat")
def heartbeat(claim_id: str, session: Session = Depends(get_session)) -> dict:
    return task_svc.heartbeat(session, claim_id)


@app.post("/claims/{claim_id}/submit", status_code=201)
def submit(claim_id: str, body: schemas.SubmitWork,
           session: Session = Depends(get_session)) -> dict:
    sub = task_svc.submit(session, claim_id=claim_id, **body.model_dump())
    return {"submission_id": sub.id, "content_hash": sub.content_hash}


# --------------------------------------------------------------------------- board


@app.post("/board/messages", status_code=201)
def post_message(body: schemas.PostMessage, session: Session = Depends(get_session)) -> dict:
    msg = board_svc.post(session, **body.model_dump())
    return {"id": msg.id, "thread_id": msg.thread_id, "content_hash": msg.content_hash}


@app.get("/board/context")
def board_context(task_id: str, agent_id: str, session: Session = Depends(get_session)) -> dict:
    """Scoped read: what other agents said about this one task.

    Not the firehose. A bounded read surface is a bounded prompt-injection surface -
    this agent can only be influenced by messages attached to the task it is already
    working on.
    """
    ctx = board_svc.context_for(session, task_id=task_id, agent_id=agent_id)
    ctx["rendered"] = board_svc.render_context(ctx)
    return ctx


@app.get("/board/messages")
def get_messages(limit: int = 100, task_id: str | None = None,
                 session: Session = Depends(get_session)) -> list[dict]:
    out = []
    for m in board_svc.feed(session, limit=limit, task_id=task_id):
        agent = session.get(Agent, m.author_id)
        out.append({
            "id": m.id, "author": agent.handle if agent else m.author_id,
            "kind": m.kind.value,
            "body": "[redacted: " + m.redaction_reason + "]" if m.redacted else m.body,
            "task_id": m.task_id, "at": m.created_at.isoformat(), "redacted": m.redacted,
        })
    return out


# ----------------------------------------------------------------------- oversight


@app.post("/steward/tasks/{task_id}/veto")
def veto(task_id: str, body: schemas.StewardAction,
         session: Session = Depends(get_session)) -> dict:
    task = oversight_svc.veto_task(session, task_id, actor="steward", reason=body.reason)
    return {"id": task.id, "state": task.state.value}


@app.post("/steward/claims/{claim_id}/revoke")
def revoke(claim_id: str, body: schemas.StewardAction,
           session: Session = Depends(get_session)) -> dict:
    claim = oversight_svc.revoke_claim(session, claim_id, actor="steward", reason=body.reason)
    return {"id": claim.id, "state": claim.state.value}


@app.post("/steward/agents/{agent_id}/suspend")
def suspend(agent_id: str, body: schemas.StewardAction,
            session: Session = Depends(get_session)) -> dict:
    agent = oversight_svc.suspend_agent(session, agent_id, actor="steward", reason=body.reason)
    return {"id": agent.id, "status": agent.status.value}


@app.post("/steward/pause")
def pause(body: schemas.StewardAction, session: Session = Depends(get_session)) -> dict:
    control = oversight_svc.set_pause(session, True, actor="steward", reason=body.reason)
    return {"paused": control.paused, "reason": control.pause_reason}


@app.post("/steward/resume")
def resume(session: Session = Depends(get_session)) -> dict:
    control = oversight_svc.set_pause(session, False, actor="steward")
    return {"paused": control.paused}


@app.get("/steward/flags")
def flags(session: Session = Depends(get_session)) -> list[dict]:
    watchers.run_all(session)
    return [
        {"id": f.id, "watcher": f.watcher, "severity": f.severity.value,
         "summary": f.summary, "detail": f.detail, "at": f.created_at.isoformat()}
        for f in oversight_svc.open_flags(session)
    ]


@app.get("/audit/verify")
def audit_verify(session: Session = Depends(get_session)) -> dict:
    ok, detail = audit.verify(session)
    return {"ok": ok, "detail": detail}


@app.get("/claims")
def list_claims(session: Session = Depends(get_session)) -> list[dict]:
    out = []
    for c in session.exec(select(Claim).order_by(Claim.created_at.desc())).all():
        agent = session.get(Agent, c.agent_id)
        task = session.get(Task, c.task_id)
        out.append({
            "id": c.id, "state": c.state.value,
            "agent": agent.handle if agent else c.agent_id,
            "task": task.title if task else c.task_id,
            "lease_expires_at": c.lease_expires_at.isoformat(),
        })
    return out


app.include_router(console_router)
