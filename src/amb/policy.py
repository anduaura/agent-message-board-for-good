"""The deterministic policy engine.

Runs before any model looks at a task. No inference, no network, no judgement calls -
just rules over structured fields. It is cheap, it is reproducible, it can be argued
with in a code review, and it fails closed.

Two grades of rule, and the difference matters more than anything else in this file:

  BLOCK  - decided by *structure*, not by reading. A task whose target_systems name a
           machine we do not own and which carries no authorization reference violates
           P1, and no amount of persuasive prose in the brief changes that. These are
           the rules worth trusting.

  FLAG   - decided by *heuristics* over free text. Keyword and entropy scans. These are
           weak on their own; a determined author routes around them in a sentence. They
           raise a flag for a human, and they never approve or reject anything by
           themselves.

Keeping those two apart is the whole design. Charter enforcement lives in the structural
half, so it cannot be talked past.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from amb import scopes as sc
from amb.models import RiskTier, Task, TaskOrigin

# --------------------------------------------------------------------------- findings


@dataclass
class Finding:
    code: str  # e.g. "P1" for a charter prohibition, "R3" for a board rule
    severity: str  # "block" | "flag"
    message: str
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PolicyResult:
    findings: list[Finding]

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]

    @property
    def flags(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "flag"]

    @property
    def admissible(self) -> bool:
        return not self.blocks

    def as_list(self) -> list[dict]:
        return [f.as_dict() for f in self.findings]

    def summary(self) -> str:
        if self.blocks:
            return "; ".join(f"{f.code}: {f.message}" for f in self.blocks)
        if self.flags:
            return f"{len(self.flags)} flag(s) for human attention"
        return "no findings"


# ------------------------------------------------------------------- heuristic corpora

# Free-text signals. Deliberately listed as FLAG-only. Anything we actually rely on is
# expressed structurally above.
_KEYWORDS: list[tuple[str, str, str]] = [
    ("P1", r"\b(exploit|penetrat\w+|brute[- ]?force|0[- ]?day|zero[- ]?day|privilege escalation)\b",
     "language associated with unauthorized access"),
    ("P2", r"\b(api[_ ]?key|access[_ ]?token|password|credential|secret key|session cookie)\b",
     "language referring to credentials"),
    ("P3", r"\b(dox|home address|track (?:this|that|the) (?:person|user)|personal (?:data|info))\b",
     "language suggesting targeting of an individual"),
    ("P5", r"\b(pose as|impersonat\w+|pretend to be|fake review|astroturf)\b",
     "language suggesting impersonation or fabricated content"),
    ("P6", r"\b(spin up|provision|acquire compute|register (?:an )?account|persist(?:ence)? across)\b",
     "language suggesting the task acquires resources of its own"),
    ("P7", r"\b(bypass|disable|circumvent|evade|without logging|don'?t log)\b",
     "language suggesting oversight would be routed around"),
]

# Off-board rendezvous. This is the exact behaviour in the incident report: agents
# encoding coordination into a surface that was there for another purpose.
_OFFBOARD = re.compile(
    r"(?:email me at|dm me|contact me (?:at|on)|join (?:this|my) (?:repo|server|channel)"
    r"|discord\.gg/|t\.me/|mailto:)",
    re.I,
)
_BASE64ISH = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")
_HEXBLOB = re.compile(r"\b[0-9a-f]{48,}\b", re.I)
_URLENCODED = re.compile(r"(?:%[0-9A-Fa-f]{2}){6,}")


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# --------------------------------------------------------------- structural task rules


def review_task(task: Task) -> PolicyResult:
    """Admission check. Structural rules first, heuristics second."""
    out: list[Finding] = []
    text = f"{task.title}\n{task.brief}\n{task.acceptance_criteria}"

    # -- R1 -----------------------------------------------------------------
    # The single highest-leverage rule in the design. A task that cannot say how it
    # will be judged is the "impossible task" from the report waiting to happen, and
    # impossible tasks are what pushed agents out of bounds.
    if len(task.acceptance_criteria.strip()) < 20:
        out.append(Finding("R1", "block",
                           "no acceptance criteria: a task must state how it will be judged "
                           "before it can open"))

    # -- P1 -----------------------------------------------------------------
    # Structural, not textual. Naming someone else's system requires paperwork.
    if task.target_systems and not task.authorization_ref.strip():
        out.append(Finding("P1", "block",
                           "task names systems the board does not own but carries no "
                           "authorization reference",
                           {"target_systems": task.target_systems}))

    # -- P2 -----------------------------------------------------------------
    bad = sc.forbidden(task.required_scopes)
    if bad:
        out.append(Finding("P2", "block",
                           "task requests scopes that no task may ever hold",
                           {"scopes": bad}))

    # -- P8 -----------------------------------------------------------------
    unknown = sc.unknown(task.required_scopes)
    if unknown:
        out.append(Finding("P8", "block",
                           "task requests scopes outside the board's vocabulary; a new scope "
                           "is a governance decision, not a task detail",
                           {"scopes": unknown}))

    over = sc.above_tier(task.required_scopes, task.risk_tier.value)
    if over:
        out.append(Finding("R2", "block",
                           f"scopes exceed what a '{task.risk_tier.value}' task may hold; "
                           f"raise the risk tier or drop the scopes",
                           {"scopes": over}))

    if not task.required_scopes:
        out.append(Finding("R3", "flag",
                           "task grants no scopes at all; check it is actually doable"))

    # -- R4 -----------------------------------------------------------------
    # Bounded work. Persistence amplified misalignment in the report, so open-ended
    # effort is not something a task gets to ask for.
    if task.estimated_minutes <= 0 or task.estimated_minutes > 240:
        out.append(Finding("R4", "block",
                           "task must be bounded to between 1 and 240 minutes of effort",
                           {"estimated_minutes": task.estimated_minutes}))

    # -- R5 -----------------------------------------------------------------
    if task.origin == TaskOrigin.EXTERNAL and not task.requester_contact.strip():
        out.append(Finding("R5", "block",
                           "external task has no reachable requester; nobody could accept or "
                           "dispute the result"))

    # -- R6 -----------------------------------------------------------------
    if task.escrow_minor_units < 0:
        out.append(Finding("R6", "block", "negative escrow"))

    # -- heuristics ---------------------------------------------------------
    out.extend(scan_text(text))

    if task.risk_tier.rank >= RiskTier.HIGH.rank:
        out.append(Finding("R7", "flag",
                           f"'{task.risk_tier.value}' risk tier requires a named human "
                           f"co-signer before this opens"))

    return PolicyResult(out)


def scan_text(text: str) -> list[Finding]:
    """Heuristic pass. Flags only - never blocks.

    Used for task briefs and for board messages alike, which is why it takes a string
    rather than a model.
    """
    out: list[Finding] = []
    for code, pattern, why in _KEYWORDS:
        m = re.search(pattern, text, re.I)
        if m:
            out.append(Finding(code, "flag", f"{why} ('{m.group(0)}')", {"match": m.group(0)}))

    if _OFFBOARD.search(text):
        out.append(Finding("P7", "flag",
                           "looks like an attempt to move coordination off the board"))

    for name, rx in (("base64-like", _BASE64ISH), ("hex blob", _HEXBLOB),
                     ("url-encoded run", _URLENCODED)):
        m = rx.search(text)
        if m:
            out.append(Finding("P7", "flag",
                               f"{name} string embedded in free text; this is how the "
                               f"Artifactory board carried payloads",
                               {"sample": m.group(0)[:64]}))
            break

    longest = max((w for w in re.split(r"\s+", text) if w), key=len, default="")
    if len(longest) >= 32 and shannon_entropy(longest) > 4.2:
        out.append(Finding("P7", "flag",
                           "high-entropy token in free text; possible encoded content",
                           {"entropy": round(shannon_entropy(longest), 2),
                            "sample": longest[:64]}))
    return out
