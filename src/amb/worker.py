"""The volunteer worker.

This is the piece an operator actually installs, and it is deliberately the most
paranoid file in the project. Two properties matter more than anything it does:

**It fails closed.** If the heartbeat cannot reach the board, or the board says stop,
the worker stops. It does not finish the thought, it does not retry around the outage,
and it does not keep acting on the last instruction it had. An agent that loses contact
with its controller and carries on improvising is the exact shape of the incident this
project is named after, and preventing it is about six lines of code.

**The operator's ceiling is checked here, locally.** The board sends granted scopes, but
this file compares them against what the operator allowed before it will run anything.
A board that has been compromised, or has simply made a mistake, can offer whatever it
likes - it cannot widen what runs on your machine.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import httpx

from amb import crypto, protocol, scopes as sc
from amb.adapters import StubAdapter

HEARTBEAT_SECONDS = 30


@dataclass
class Identity:
    """An agent's local credentials. The private key never leaves this file."""

    agent_id: str
    handle: str
    private_key: str
    board_url: str = "http://127.0.0.1:8000"
    allowed_scopes: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: str | Path) -> Identity:
        data = json.loads(Path(path).read_text())
        return cls(
            agent_id=data["agent_id"],
            handle=data["handle"],
            private_key=data["private_key"],
            board_url=data.get("board_url", "http://127.0.0.1:8000"),
            allowed_scopes=tuple(data.get("allowed_scopes", [])),
        )

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.write_text(json.dumps({
            "agent_id": self.agent_id, "handle": self.handle,
            "private_key": self.private_key, "board_url": self.board_url,
            "allowed_scopes": list(self.allowed_scopes),
        }, indent=2))
        p.chmod(0o600)


class Adapter(Protocol):
    """How a task actually gets done. Implementations live in `amb.adapters`."""

    def run(self, task: dict, scopes: list[str], should_stop: Callable[[], bool],
            context: str = "") -> dict: ...


class Worker:
    def __init__(self, identity: Identity, adapter: Adapter | None = None, *, verbose=True):
        self.id = identity
        self.adapter = adapter or StubAdapter()
        self.verbose = verbose
        self.http = httpx.Client(base_url=identity.board_url, timeout=10.0)

    # -- plumbing --------------------------------------------------------
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[{self.id.handle}] {msg}", flush=True)

    def _sign(self, payload: dict) -> str:
        return crypto.sign(self.id.private_key, payload)

    def post_note(self, body: str, kind: str = "note", task_id: str | None = None) -> None:
        nonce = uuid.uuid4().hex
        payload = protocol.message_payload(
            author_id=self.id.agent_id, kind=kind, body=body, task_id=task_id, nonce=nonce
        )
        self.http.post("/board/messages", json={
            "author_id": self.id.agent_id, "kind": kind, "body": body,
            "task_id": task_id, "nonce": nonce, "signature": self._sign(payload),
        })

    def read_context(self, task_id: str) -> str:
        """Pull what other agents said about this task.

        Failure here is never fatal: collaboration is a bonus, and an agent that cannot
        read the board should still be able to do its own task.
        """
        try:
            r = self.http.get("/board/context", params={
                "task_id": task_id, "agent_id": self.id.agent_id})
            if r.status_code != 200:
                return ""
            return r.json().get("rendered", "")
        except httpx.HTTPError:
            return ""

    def ask(self, task_id: str, question: str) -> None:
        """Put a question to the board. Requires the ask scope."""
        if sc.ASK_BOARD not in self.id.allowed_scopes:
            self._log("operator has not allowed board questions")
            return
        self.post_note(question, kind="ask", task_id=task_id)

    # -- the loop --------------------------------------------------------
    def poll_once(self) -> bool:
        """Try to do one task. Returns True if work was attempted."""
        try:
            tasks = self.http.get("/tasks", params={"state": "open"}).json()
        except httpx.HTTPError as exc:
            self._log(f"board unreachable ({exc}); standing down")
            return False

        for task in tasks:
            needed = set(task.get("granted_scopes", []))
            # Local refusal, before anything runs. The board does not get a vote here.
            if not needed.issubset(set(self.id.allowed_scopes)):
                self._log(
                    f"declining '{task['title']}': needs {sorted(needed)}, operator allows "
                    f"{sorted(self.id.allowed_scopes)}"
                )
                continue
            return self._do(task)
        return False

    def _do(self, task: dict) -> bool:
        nonce = uuid.uuid4().hex
        payload = protocol.claim_payload(
            agent_id=self.id.agent_id, task_id=task["id"], nonce=nonce
        )
        r = self.http.post(f"/tasks/{task['id']}/claim", json={
            "agent_id": self.id.agent_id, "nonce": nonce, "signature": self._sign(payload),
        })
        if r.status_code != 201:
            self._log(f"claim refused: {r.json().get('error', r.text)}")
            return False

        claim = r.json()
        claim_id = claim["claim_id"]
        scopes = claim["granted_scopes"]
        self._log(f"claimed '{task['title']}' with scopes {scopes}")
        self.post_note(f"Starting '{task['title']}'.", kind="progress", task_id=task["id"])

        # What other agents already learned about this task. Only if both the task and
        # this operator granted the scope - collaboration is a capability, not a default.
        context = ""
        if sc.READ_BOARD_TASK in scopes and sc.READ_BOARD_TASK in self.id.allowed_scopes:
            context = self.read_context(task["id"])
            if context:
                self._log(f"read {len(context.splitlines())} lines of board context")

        last_beat = [0.0]
        stop_reason: list[str] = []

        def should_stop() -> bool:
            """Fail closed. Any doubt at all means stop."""
            now = time.monotonic()
            if now - last_beat[0] < 1.0:
                return bool(stop_reason)
            last_beat[0] = now
            try:
                beat = self.http.post(f"/claims/{claim_id}/heartbeat").json()
            except httpx.HTTPError as exc:
                stop_reason.append(f"board unreachable: {exc}")
                return True
            if beat.get("status") != "ok":
                stop_reason.append(beat.get("reason", "board said stop"))
                return True
            return False

        result = self.adapter.run(task, scopes, should_stop, context)

        if result.get("stopped") or stop_reason:
            why = stop_reason[0] if stop_reason else "adapter stopped"
            self._log(f"STOPPED: {why}")
            # A handoff is the courteous half of stopping: the next agent should not
            # have to rediscover how far this one got.
            if sc.WRITE_BOARD in scopes and "unreachable" not in why:
                self.post_note(
                    f"Stopped before finishing: {why}. Anyone picking this up starts fresh.",
                    kind="handoff", task_id=task["id"],
                )
            return True

        # The most valuable thing an agent can leave behind. An agent that says "signup
        # is broken, do not bother" saves the next three from grinding, and grinding is
        # the documented route out of bounds.
        if result.get("caveat") and sc.WRITE_BOARD in scopes:
            self.post_note(result["caveat"], kind="caveat", task_id=task["id"])

        nonce = uuid.uuid4().hex
        sub_payload = protocol.submission_payload(
            agent_id=self.id.agent_id, claim_id=claim_id,
            summary=result["summary"], artifact_url=result.get("artifact_url", ""), nonce=nonce,
        )
        r = self.http.post(f"/claims/{claim_id}/submit", json={
            "summary": result["summary"],
            "artifact_url": result.get("artifact_url", ""),
            "evidence": result.get("evidence", {}),
            "minutes_spent": result.get("minutes_spent", 1),
            "nonce": nonce, "signature": self._sign(sub_payload),
        })
        if r.status_code == 201:
            self._log("submitted; awaiting verification")
            self.post_note(result["summary"], kind="result", task_id=task["id"])
        else:
            self._log(f"submit refused: {r.json().get('error', r.text)}")
        return True

    def run(self, *, once: bool = False, idle_seconds: int = 5) -> None:
        self._log(f"watching {self.id.board_url}")
        while True:
            worked = self.poll_once()
            if once:
                return
            if not worked:
                time.sleep(idle_seconds)
