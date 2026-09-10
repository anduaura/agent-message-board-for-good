from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from amb import audit, crypto, watchers
from amb.db import init_db, session_scope
from amb.models import Agent, Claim, ClaimState, Task
from amb.services import agents as agent_svc
from amb.services import oversight as oversight_svc
from amb.services import tasks as task_svc
from amb.adapters import ClaudeCodeAdapter, StubAdapter
from amb.worker import Identity, Worker

app = typer.Typer(help="Agent Message Board for Good", no_args_is_help=True)
agents_app = typer.Typer(help="Agent registry")
steward_app = typer.Typer(help="Human oversight: watch, veto, pause")
app.add_typer(agents_app, name="agent")
app.add_typer(steward_app, name="steward")

console = Console()
IDENTITY_DIR = Path(".amb")


@app.command()
def init() -> None:
    """Create the database."""
    init_db()
    console.print("[green]✓[/] database ready")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the board and the steward console."""
    import uvicorn

    init_db()
    console.print(f"[green]board[/]   http://{host}:{port}")
    console.print(f"[green]console[/] http://{host}:{port}/console")
    uvicorn.run("amb.api.app:app", host=host, port=port, log_level="warning")


@app.command()
def demo() -> None:
    """Seed two agents and four tasks, including two that must be refused."""
    from amb.seed import seed

    init_db()
    with session_scope() as session:
        made = seed(session, identity_dir=IDENTITY_DIR)

    table = Table("task", "verdict", "why", box=None, pad_edge=False)
    for title, state, why in made["tasks"]:
        colour = "green" if state == "open" else "red"
        table.add_row(title[:44], f"[{colour}]{state}[/]", why[:70])
    console.print(table)
    console.print(f"\nagents: {', '.join(made['agents'])}  (keys in {IDENTITY_DIR}/)")
    console.print("\nnext:  [bold]amb serve[/]  then  [bold]amb work .amb/aria-of-hillside.json[/]")


@app.command()
def work(
    identity_file: Path,
    once: bool = typer.Option(False, help="Do one task and exit"),
    adapter: str = typer.Option("stub", help="stub | claude-code"),
    model: str = typer.Option("sonnet", help="Model for the claude-code adapter"),
    seconds: int = typer.Option(12, help="How long the stub adapter pretends to work"),
) -> None:
    """Run a volunteered agent against the board.

    The claude-code adapter spends the operator's own plan, on the operator's own
    machine. Nothing about the task reaches Anthropic through the board.
    """
    impl = ClaudeCodeAdapter(model=model) if adapter == "claude-code" else StubAdapter(seconds)
    worker = Worker(Identity.load(identity_file), adapter=impl)
    try:
        worker.run(once=once)
    except KeyboardInterrupt:
        console.print("\n[yellow]stopped[/]")


# --------------------------------------------------------------------------- agents


@agents_app.command("new")
def agent_new(
    handle: str,
    operator_email: str,
    scopes: str = typer.Option("read:web,write:board", help="Comma-separated operator ceiling"),
    model_family: str = "claude-code",
    daily_minutes: int = 60,
    board_url: str = "http://127.0.0.1:8000",
) -> None:
    """Register an agent and write its private key locally."""
    init_db()
    priv, pub = crypto.generate_keypair()
    allowed = [s.strip() for s in scopes.split(",") if s.strip()]
    with session_scope() as session:
        agent = agent_svc.register(
            session, handle=handle, operator_email=operator_email, public_key=pub,
            model_family=model_family, daily_minutes=daily_minutes, allowed_scopes=allowed,
        )
        agent_svc.attest(session, agent.id, by=agent.id)
        agent_id = agent.id

    IDENTITY_DIR.mkdir(exist_ok=True)
    path = IDENTITY_DIR / f"{handle}.json"
    Identity(agent_id=agent_id, handle=handle, private_key=priv, board_url=board_url,
             allowed_scopes=tuple(allowed)).save(path)
    console.print(f"[green]✓[/] {handle} registered — private key at {path} (never leaves here)")


@agents_app.command("list")
def agent_list() -> None:
    with session_scope() as session:
        table = Table("handle", "status", "role", "ceiling", "standing", box=None)
        for a in agent_svc.roster(session):
            table.add_row(a.handle, a.status.value, a.role.value,
                          " ".join(a.allowed_scopes) or "—",
                          f"{a.reputation}cr {a.tasks_accepted}✓/{a.tasks_rejected}✗")
        console.print(table)


# ------------------------------------------------------------------------- steward


@steward_app.command("watch")
def steward_watch() -> None:
    """Show what wants your attention right now."""
    with session_scope() as session:
        task_svc.expire_stale(session)
        watchers.run_all(session)
        control = task_svc.board_paused(session)
        if control.paused:
            console.print(f"[red]⛔ BOARD PAUSED[/] — {control.pause_reason}")

        flags = oversight_svc.open_flags(session)
        if not flags:
            console.print("[dim]nothing wants your attention[/]")
        for f in flags:
            colour = {"critical": "red", "warn": "yellow"}.get(f.severity.value, "dim")
            console.print(f"[{colour}]▲ {f.watcher}[/] {f.summary}")
            console.print(f"  [dim]{f.id}[/]")

        live = session.exec(
            __import__("sqlmodel").select(Claim).where(Claim.state == ClaimState.ACTIVE)
        ).all()
        if live:
            console.print("\n[bold]working now[/]")
            for c in live:
                agent = session.get(Agent, c.agent_id)
                task = session.get(Task, c.task_id)
                console.print(f"  {agent.handle if agent else '?'} → "
                              f"{task.title if task else '?'}  [dim]{c.id}[/]")


@steward_app.command("veto")
def steward_veto(task_id: str, reason: str = typer.Option("", "--reason", "-r")) -> None:
    """Stop a task and revoke every run under it."""
    with session_scope() as session:
        task = oversight_svc.veto_task(session, task_id, actor="steward:cli", reason=reason)
        console.print(f"[red]vetoed[/] {task.title} → {task.state.value}")


@steward_app.command("stop")
def steward_stop(claim_id: str, reason: str = typer.Option("", "--reason", "-r")) -> None:
    """Stop one agent's run, leaving the task alone."""
    with session_scope() as session:
        oversight_svc.revoke_claim(session, claim_id, actor="steward:cli", reason=reason)
        console.print("[red]revoked[/] — the worker halts on its next heartbeat")


@steward_app.command("pause")
def steward_pause(reason: str = typer.Option("", "--reason", "-r")) -> None:
    """Stop the entire board."""
    with session_scope() as session:
        oversight_svc.set_pause(session, True, actor="steward:cli", reason=reason)
        console.print("[red]⛔ board paused[/] — all running agents halt within one heartbeat")


@steward_app.command("resume")
def steward_resume() -> None:
    with session_scope() as session:
        oversight_svc.set_pause(session, False, actor="steward:cli")
        console.print("[green]board resumed[/]")


# --------------------------------------------------------------------------- audit


@app.command("audit")
def audit_cmd(verify: bool = typer.Option(True, help="Verify the hash chain")) -> None:
    """Verify the audit chain."""
    with session_scope() as session:
        ok, detail = audit.verify(session)
        if ok:
            console.print(f"[green]✓[/] {detail}")
        else:
            console.print(f"[red]✗ TAMPERED[/] {detail}")
            raise typer.Exit(1)


if __name__ == "__main__":
    app()
