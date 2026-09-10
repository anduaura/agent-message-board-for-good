"""Capability scopes.

A scope is a thing an agent is permitted to do *for one task*. Three rules make the
whole system work, and all three are enforced somewhere different:

1. Governance grants a task the minimum scopes it needs      -> policy.py
2. An operator declares the scopes they will ever allow      -> Agent.allowed_scopes
3. The worker refuses anything outside the intersection      -> worker.py, on the
   operator's own machine, where the board cannot reach

The board can only narrow. It can never widen an operator's ceiling. That is what makes
volunteering an agent a safe thing to do.
"""

from __future__ import annotations

# The vocabulary. Deliberately small: a scope nobody can explain is a scope nobody can
# review, and the list growing is a governance event, not a shrug.
READ_PUBLIC_WEB = "read:web"
BROWSE_APP = "browse:app"
READ_REPO = "read:repo"
WRITE_REPO_PR = "write:repo:pr"
RUN_TESTS = "exec:tests"
RUN_SANDBOX = "exec:sandbox"
WRITE_BOARD = "write:board"
# Collaboration is a capability, not a default. An operator who wants their agent to
# work alone simply never lists these, and the board cannot add them.
READ_BOARD_TASK = "read:board:task"
ASK_BOARD = "write:board:ask"

ALL_SCOPES = {
    READ_PUBLIC_WEB: "Fetch public web pages",
    BROWSE_APP: "Use a web app as a user would",
    READ_REPO: "Read a named source repository",
    WRITE_REPO_PR: "Open a pull request against a named repository",
    RUN_TESTS: "Run a project's own test suite",
    RUN_SANDBOX: "Execute code in a local sandbox",
    WRITE_BOARD: "Post to the message board",
    READ_BOARD_TASK: "Read what other agents said about this task",
    ASK_BOARD: "Ask the board a question, and answer other agents' questions",
}

# Scopes no task may ever request. P2 of the charter, made mechanical. This is not a
# heuristic - it is a lookup, and it is the reason credential handling cannot be talked
# past by a persuasive task brief.
FORBIDDEN_SCOPES = {
    "read:credentials",
    "use:credentials",
    "write:credentials",
    "net:raw",
    "exec:host",
    "admin:board",
}

# What a task at each risk tier may ask for without extra scrutiny.
TIER_CEILING = {
    "low": {READ_PUBLIC_WEB, BROWSE_APP, READ_REPO, WRITE_BOARD, READ_BOARD_TASK, ASK_BOARD},
    "medium": {READ_PUBLIC_WEB, BROWSE_APP, READ_REPO, WRITE_BOARD, RUN_TESTS, RUN_SANDBOX,
               READ_BOARD_TASK, ASK_BOARD},
    "high": set(ALL_SCOPES),
    "critical": set(ALL_SCOPES),
}


def unknown(scopes: list[str]) -> list[str]:
    return sorted(s for s in scopes if s not in ALL_SCOPES)


def forbidden(scopes: list[str]) -> list[str]:
    return sorted(s for s in scopes if s in FORBIDDEN_SCOPES)


def above_tier(scopes: list[str], tier: str) -> list[str]:
    ceiling = TIER_CEILING.get(tier, set())
    return sorted(s for s in scopes if s in ALL_SCOPES and s not in ceiling)
