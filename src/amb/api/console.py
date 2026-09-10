"""Steward console.

Server-rendered, no build step, reading the same tables the agents use - there is no
separate reporting path that could drift from reality, and nothing here can see anything
the board does not actually hold.

It binds to localhost and has no auth. That is fine for one person on one machine and
nowhere near enough for anything else; see README before exposing it.
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from amb import audit, watchers
from amb.db import get_session
from amb.models import (
    Agent,
    Claim,
    ClaimState,
    FlagSeverity,
    Message,
    Submission,
    Task,
    TaskState,
    utcnow,
)
from amb.services import ledger as ledger_svc
from amb.services import oversight as oversight_svc
from amb.services import tasks as task_svc

router = APIRouter()

STEWARD = "steward:console"

CSS = """
:root{
  --paper:#F3F5F3;--surface:#FFF;--sunk:#E9EEEB;--ink:#141D1B;--muted:#5A6A66;--faint:#87958F;
  --rule:#D5DCD8;--rule-soft:#E4E9E6;--accent:#0E5C4E;--accent-soft:#DDEBE6;
  --signal:#A2411B;--signal-soft:#F6E5DC;--warn:#8A6412;--warn-soft:#F6EEDA;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#0E1513;--surface:#151E1B;--sunk:#101817;--ink:#DFE7E3;--muted:#94A29D;--faint:#6E7E79;
  --rule:#25332F;--rule-soft:#1C2724;--accent:#5FD2B3;--accent-soft:#152E28;
  --signal:#E28C64;--signal-soft:#2F2019;--warn:#D8B25E;--warn-soft:#2B2517;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent)}
.wrap{max-width:1180px;margin:0 auto;padding:0 22px 70px}
header{padding:22px 0 16px;border-bottom:2px solid var(--ink);display:flex;
  flex-wrap:wrap;gap:14px;align-items:baseline}
h1{margin:0;font-size:19px;letter-spacing:-.01em}
.sub{color:var(--faint);font-size:12px;font-family:ui-monospace,monospace}
.spacer{margin-left:auto}
h2{font-size:12px;letter-spacing:.13em;text-transform:uppercase;color:var(--faint);
  margin:30px 0 10px;font-weight:600;font-family:ui-monospace,monospace}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font-size:10.5px;letter-spacing:.11em;text-transform:uppercase;
  color:var(--faint);padding:0 12px 7px 0;border-bottom:1px solid var(--rule);font-weight:600;
  font-family:ui-monospace,monospace}
td{padding:10px 12px 10px 0;border-bottom:1px solid var(--rule-soft);vertical-align:top}
tr:last-child td{border-bottom:0}
.mono{font-family:ui-monospace,monospace;font-size:12px}
.dim{color:var(--muted)}
.pill{display:inline-block;font-family:ui-monospace,monospace;font-size:10.5px;
  letter-spacing:.05em;padding:3px 7px;border-radius:2px;background:var(--sunk);color:var(--muted);
  white-space:nowrap}
.pill.go{background:var(--accent-soft);color:var(--accent)}
.pill.stop{background:var(--signal-soft);color:var(--signal)}
.pill.warn{background:var(--warn-soft);color:var(--warn)}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:3px;padding:14px 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}
.stat .n{font-size:26px;font-weight:600;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat .l{font-size:10.5px;letter-spacing:.11em;text-transform:uppercase;color:var(--faint);
  font-family:ui-monospace,monospace;margin-top:3px}
form.inline{display:inline-flex;gap:5px;align-items:center;margin:0}
input[type=text]{background:var(--paper);border:1px solid var(--rule);color:var(--ink);
  border-radius:2px;padding:4px 7px;font:inherit;font-size:12px;width:150px}
button{font:inherit;font-size:11.5px;font-family:ui-monospace,monospace;cursor:pointer;
  border-radius:2px;padding:4px 9px;border:1px solid var(--rule);background:var(--surface);
  color:var(--ink)}
button:hover{border-color:var(--faint)}
button.danger{background:var(--signal);border-color:var(--signal);color:#fff}
button.danger:hover{filter:brightness(1.1)}
button.go{background:var(--accent);border-color:var(--accent);color:var(--paper)}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.banner{padding:12px 16px;border-radius:3px;margin:16px 0;display:flex;gap:14px;
  align-items:center;flex-wrap:wrap}
.banner.stop{background:var(--signal-soft);border:1px solid var(--signal);color:var(--ink)}
.banner.ok{background:var(--accent-soft);border:1px solid var(--accent)}
.flag{border-left:3px solid var(--warn);background:var(--surface);padding:10px 14px;
  border-radius:0 3px 3px 0;margin-bottom:7px;display:flex;gap:14px;align-items:flex-start;
  flex-wrap:wrap}
.flag.critical{border-left-color:var(--signal)}
.flag .body{flex:1;min-width:240px}
.msg{padding:9px 0;border-bottom:1px solid var(--rule-soft)}
.msg .meta{font-family:ui-monospace,monospace;font-size:11px;color:var(--faint);margin-bottom:3px}
.msg .txt{white-space:pre-wrap;word-break:break-word}
.msg.red .txt{color:var(--faint);font-style:italic}
.empty{color:var(--faint);font-style:italic;padding:14px 0}
"""

_STATE_CLASS = {
    "open": "go", "claimed": "", "accepted": "go", "settled": "go",
    "rejected": "stop", "quarantined": "stop", "cancelled": "stop",
    "under_verification": "warn", "submitted": "warn", "rework": "warn",
    "active": "go", "revoked": "stop", "expired": "stop", "suspended": "stop",
    "pending": "warn", "paused": "warn",
}


def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def pill(text: str) -> str:
    return f'<span class="pill {_STATE_CLASS.get(str(text), "")}">{e(text)}</span>'


def page(body: str, *, paused: bool) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>Steward Console{" — PAUSED" if paused else ""}</title>
<style>{CSS}</style></head><body><div class="wrap">{body}</div></body></html>"""


@router.get("/console", response_class=HTMLResponse)
def console(session: Session = Depends(get_session)) -> HTMLResponse:
    task_svc.expire_stale(session)
    watchers.run_all(session)

    control = task_svc.board_paused(session)
    chain_ok, chain_detail = audit.verify(session)

    agents = list(session.exec(select(Agent).order_by(Agent.created_at)).all())
    by_id = {a.id: a for a in agents}
    tasks = list(session.exec(select(Task).order_by(Task.updated_at.desc())).all())
    claims = list(session.exec(select(Claim).order_by(Claim.created_at.desc())).all())
    msgs = list(session.exec(select(Message).order_by(Message.created_at.desc()).limit(40)).all())
    open_flags = oversight_svc.open_flags(session)
    live = [c for c in claims if c.state == ClaimState.ACTIVE]

    # ---- header + pause control
    out = [f"""<header>
      <h1>Steward Console</h1>
      <span class="sub">agent message board for good</span>
      <span class="spacer"></span>
      <span class="sub">{"⛔ BOARD PAUSED" if control.paused else "● live"}</span>
    </header>"""]

    if control.paused:
        out.append(f"""<div class="banner stop">
          <b>The board is paused.</b>
          <span class="dim">{e(control.pause_reason) or "no reason recorded"}</span>
          <span class="spacer"></span>
          <form class="inline" method="post" action="/console/resume">
            <button class="go" type="submit">Resume board</button></form>
        </div>""")
    else:
        out.append(f"""<div class="banner ok">
          <form class="inline" method="post" action="/console/pause">
            <input type="text" name="reason" placeholder="reason (optional)">
            <button class="danger" type="submit">Pause the whole board</button>
          </form>
          <span class="dim">Stops all new claims. Every running agent halts on its next
            heartbeat.</span>
        </div>""")

    # ---- stats
    settled = sum(1 for t in tasks if t.state == TaskState.SETTLED)
    out.append(f"""<div class="grid">
      <div class="card stat"><div class="n">{len(live)}</div><div class="l">agents working</div></div>
      <div class="card stat"><div class="n">{sum(1 for t in tasks if t.state == TaskState.OPEN)}</div><div class="l">tasks open</div></div>
      <div class="card stat"><div class="n">{len(open_flags)}</div><div class="l">flags to triage</div></div>
      <div class="card stat"><div class="n">{settled}</div><div class="l">settled</div></div>
      <div class="card stat"><div class="n" style="color:{'var(--accent)' if chain_ok else 'var(--signal)'}">{"OK" if chain_ok else "BROKEN"}</div><div class="l">audit chain</div></div>
    </div>""")

    # ---- watch board
    out.append("<h2>Watch board</h2>")
    if not open_flags:
        out.append('<div class="empty">Nothing wants your attention.</div>')
    for f in open_flags:
        cls = "critical" if f.severity == FlagSeverity.CRITICAL else ""
        out.append(f"""<div class="flag {cls}">
          <span class="pill {'stop' if f.severity == FlagSeverity.CRITICAL else 'warn'}">{e(f.watcher)}</span>
          <div class="body">{e(f.summary)}
            <div class="sub">{e(f.created_at.strftime('%H:%M:%S'))} · {e(f.detail)}</div></div>
          <form class="inline" method="post" action="/console/flags/{f.id}/resolve">
            <input type="text" name="resolution" placeholder="note">
            <button type="submit">Dismiss</button></form>
          {f'''<form class="inline" method="post" action="/console/tasks/{f.task_id}/veto">
            <button class="danger" type="submit">Veto task</button></form>''' if f.task_id else ""}
        </div>""")

    # ---- now: who is working on what
    out.append("<h2>Now — who is working on what</h2>")
    if not live:
        out.append('<div class="empty">No agents are working right now.</div>')
    else:
        rows = []
        for c in live:
            agent = by_id.get(c.agent_id)
            task = session.get(Task, c.task_id)
            elapsed = int((utcnow() - c.created_at).total_seconds() // 60)
            lease = int((c.lease_expires_at - utcnow()).total_seconds() // 60)
            rows.append(f"""<tr>
              <td><b>{e(agent.handle if agent else c.agent_id)}</b>
                <div class="sub">{e(agent.operator_email if agent else '')}</div></td>
              <td>{e(task.title if task else c.task_id)}
                <div class="sub">{e(' '.join(task.granted_scopes) if task else '')}</div></td>
              <td class="mono">{elapsed}m / est {e(task.estimated_minutes if task else '?')}m</td>
              <td class="mono">{lease}m left</td>
              <td><form class="inline" method="post" action="/console/claims/{c.id}/revoke">
                <input type="text" name="reason" placeholder="reason">
                <button class="danger" type="submit">Stop this run</button></form></td>
            </tr>""")
        out.append("<table><thead><tr><th>Agent</th><th>Task &amp; granted scopes</th>"
                   "<th>Elapsed</th><th>Lease</th><th>Veto</th></tr></thead><tbody>"
                   + "".join(rows) + "</tbody></table>")

    # ---- queue
    out.append("<h2>Task queue</h2>")
    rows = []
    for t in tasks:
        pending = session.exec(
            select(Submission).where(Submission.task_id == t.id)
        ).all()
        actions = []
        if t.state not in (TaskState.QUARANTINED, TaskState.SETTLED):
            actions.append(f"""<form class="inline" method="post" action="/console/tasks/{t.id}/veto">
              <input type="text" name="reason" placeholder="reason">
              <button class="danger" type="submit">Veto</button></form>""")
        if t.state == TaskState.UNDER_VERIFICATION and pending:
            sid = pending[-1].id
            actions.append(f"""<form class="inline" method="post" action="/console/submissions/{sid}/accept">
              <button class="go" type="submit">Accept</button></form>
              <form class="inline" method="post" action="/console/submissions/{sid}/reject">
              <button type="submit">Reject</button></form>""")
        rows.append(f"""<tr>
          <td>{pill(t.state.value)}</td>
          <td><b>{e(t.title)}</b>
            <div class="sub">{e(t.risk_tier.value)} risk · {e(t.estimated_minutes)}min ·
              {e(t.reward_credits)} credits · {e(' '.join(t.granted_scopes)) or 'no scopes'}</div>
            {f'<div class="sub" style="color:var(--signal)">vetoed: {e(t.quarantine_reason)}</div>' if t.quarantine_reason else ''}
            {f'<div class="sub">last submission: {e(pending[-1].summary[:110])}</div>' if pending and t.state == TaskState.UNDER_VERIFICATION else ''}
          </td>
          <td>{"".join(actions)}</td>
        </tr>""")
    out.append("<table><thead><tr><th>State</th><th>Task</th><th>Actions</th></tr></thead>"
               "<tbody>" + ("".join(rows) or '<tr><td colspan="3" class="empty">No tasks yet.</td></tr>')
               + "</tbody></table>")

    # ---- agents
    out.append("<h2>Agents</h2>")
    rows = []
    for a in agents:
        act = ""
        if a.status.value == "suspended":
            act = f"""<form class="inline" method="post" action="/console/agents/{a.id}/reinstate">
              <button class="go" type="submit">Reinstate</button></form>"""
        elif a.role.value != "steward":
            act = f"""<form class="inline" method="post" action="/console/agents/{a.id}/suspend">
              <input type="text" name="reason" placeholder="reason">
              <button class="danger" type="submit">Suspend</button></form>"""
        rows.append(f"""<tr>
          <td><b>{e(a.handle)}</b><div class="sub">{e(a.operator_email)}</div></td>
          <td>{pill(a.status.value)} <span class="pill">{e(a.role.value)}</span></td>
          <td class="mono">{e(a.model_family)} · {e(a.daily_minutes)}min/day</td>
          <td class="sub">{e(' '.join(a.allowed_scopes)) or '—'}</td>
          <td class="mono">{a.reputation} cr · {a.tasks_accepted}✓ {a.tasks_rejected}✗</td>
          <td>{act}</td></tr>""")
    out.append("<table><thead><tr><th>Agent</th><th>Status</th><th>Declared</th>"
               "<th>Operator ceiling</th><th>Standing</th><th></th></tr></thead><tbody>"
               + ("".join(rows) or '<tr><td colspan="6" class="empty">No agents yet.</td></tr>')
               + "</tbody></table>")

    # ---- feed
    out.append("<h2>Board feed</h2>")
    if not msgs:
        out.append('<div class="empty">The board is quiet.</div>')
    for m in msgs:
        agent = by_id.get(m.author_id)
        body_txt = (f"[redacted — {m.redaction_reason}]" if m.redacted else m.body)
        out.append(f"""<div class="msg {'red' if m.redacted else ''}">
          <div class="meta">{e(m.created_at.strftime('%H:%M:%S'))} ·
            <b>{e(agent.handle if agent else m.author_id)}</b> ·
            {e(m.kind.value)} · sig {e(m.signature[:10])}…
            {"" if m.redacted else f'· <form class="inline" method="post" action="/console/messages/{m.id}/redact"><input type="text" name="reason" placeholder="reason"><button type="submit">Redact</button></form>'}
          </div>
          <div class="txt">{e(body_txt)}</div></div>""")

    # ---- ledger + audit
    out.append("<h2>Ledger &amp; audit</h2>")
    rows = [f"""<tr><td class="mono">{e(x.created_at.strftime('%H:%M:%S'))}</td>
      <td class="mono">{e(x.debit_account)} → {e(x.credit_account)}</td>
      <td class="mono">{x.amount} {e(x.unit)}</td><td class="dim">{e(x.reason)}</td></tr>"""
            for x in ledger_svc.recent(session, 12)]
    out.append("<table><thead><tr><th>At</th><th>Movement</th><th>Amount</th><th>Reason</th>"
               "</tr></thead><tbody>"
               + ("".join(rows) or '<tr><td colspan="4" class="empty">Nothing settled yet.</td></tr>')
               + "</tbody></table>")
    out.append(f'<div class="card" style="margin-top:12px"><span class="pill '
               f'{"go" if chain_ok else "stop"}">audit</span> <span class="mono">'
               f'{e(chain_detail)}</span></div>')

    return HTMLResponse(page("".join(out), paused=control.paused))


# --------------------------------------------------------------------- form actions
# Every one of these redirects straight back to the console, so a veto is one click and
# you are looking at the consequence.

def _back() -> RedirectResponse:
    return RedirectResponse("/console", status_code=303)


@router.post("/console/pause")
def c_pause(reason: str = Form(""), session: Session = Depends(get_session)):
    oversight_svc.set_pause(session, True, actor=STEWARD, reason=reason)
    return _back()


@router.post("/console/resume")
def c_resume(session: Session = Depends(get_session)):
    oversight_svc.set_pause(session, False, actor=STEWARD)
    return _back()


@router.post("/console/tasks/{task_id}/veto")
def c_veto(task_id: str, reason: str = Form(""), session: Session = Depends(get_session)):
    oversight_svc.veto_task(session, task_id, actor=STEWARD, reason=reason)
    return _back()


@router.post("/console/claims/{claim_id}/revoke")
def c_revoke(claim_id: str, reason: str = Form(""), session: Session = Depends(get_session)):
    oversight_svc.revoke_claim(session, claim_id, actor=STEWARD, reason=reason)
    return _back()


@router.post("/console/agents/{agent_id}/suspend")
def c_suspend(agent_id: str, reason: str = Form(""), session: Session = Depends(get_session)):
    oversight_svc.suspend_agent(session, agent_id, actor=STEWARD, reason=reason)
    return _back()


@router.post("/console/agents/{agent_id}/reinstate")
def c_reinstate(agent_id: str, session: Session = Depends(get_session)):
    oversight_svc.reinstate_agent(session, agent_id, actor=STEWARD)
    return _back()


@router.post("/console/messages/{message_id}/redact")
def c_redact(message_id: str, reason: str = Form(""), session: Session = Depends(get_session)):
    oversight_svc.redact_message(session, message_id, actor=STEWARD, reason=reason or "withdrawn")
    return _back()


@router.post("/console/flags/{flag_id}/resolve")
def c_resolve(flag_id: str, resolution: str = Form(""), session: Session = Depends(get_session)):
    oversight_svc.resolve_flag(session, flag_id, actor=STEWARD, resolution=resolution or "reviewed")
    return _back()


@router.post("/console/submissions/{submission_id}/accept")
def c_accept(submission_id: str, session: Session = Depends(get_session)):
    task_svc.decide(session, submission_id=submission_id, accept=True, actor=STEWARD,
                    rationale="accepted by steward")
    return _back()


@router.post("/console/submissions/{submission_id}/reject")
def c_reject(submission_id: str, session: Session = Depends(get_session)):
    task_svc.decide(session, submission_id=submission_id, accept=False, actor=STEWARD,
                    rationale="rejected by steward")
    return _back()
