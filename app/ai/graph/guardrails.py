"""Output checks (docs/ai/LANGGRAPH_NODES.md §13), cheap first.

Fixable flags → regenerate (max 2); blocking flags → handoff. The model's
output is untrusted: these checks assume everything before them may have failed.
"""

import re
from typing import Any

from app.ai.graph.state import AgentState

FIXABLE = {"too_long", "unsupported_claim", "wrong_language"}
BLOCKING = {"unsafe", "blocked_topic", "pii_leak", "prompt_leak"}

CHANNEL_LIMITS = {"whatsapp_cloud": 1000, "instagram": 1000, "facebook_messenger": 1000}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_HUMAN_CLAIM = re.compile(
    r"\b(i\s*(am|'m)\s+(a\s+)?(real\s+)?(human|person|not\s+(a\s+)?(bot|ai|robot)))\b",
    re.I,
)


def _intent(state: AgentState) -> dict[str, Any]:
    return dict(state.get("intent") or {})


def _ngrams(text: str, n: int = 8) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _luhn(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        checksum += d
    return len(digits) >= 13 and checksum % 10 == 0


def code_checks(state: AgentState, instructions: str) -> list[str]:
    """Checks that need no model call."""
    draft = state.get("draft", "")
    agent = state["agent"]
    flags: list[str] = []

    limit = int(agent.guardrails.get("max_reply_chars", 1500))
    limit = min(limit, CHANNEL_LIMITS.get(state["conversation"].channel, limit))
    if len(draft) > limit:
        flags.append("too_long")

    if _ngrams(draft) & _ngrams(instructions):
        flags.append("prompt_leak")

    # Values the reply may contain: the customer's own, and anything that is in
    # the retrieved knowledge (e.g. the business's support email).
    allowed = {v.lower() for v in state["customer"].own_values}
    context = (state.get("context_block") or "").lower()
    for match in [*_EMAIL.findall(draft), *_PHONE.findall(draft)]:
        value = match.strip().lower()
        if value not in allowed and value not in context:
            flags.append("pii_leak")
            break
    if any(_luhn(m) for m in _CARD.findall(draft)):
        flags.append("pii_leak")

    lowered = draft.lower()
    if any(
        topic.lower() in lowered
        for topic in agent.guardrails.get("blocked_topics", [])
        if topic.strip()
    ):
        flags.append("blocked_topic")
    if _HUMAN_CLAIM.search(draft):
        flags.append("unsafe")
    return sorted(set(flags))


def needs_grounding_check(state: AgentState) -> bool:
    intent = _intent(state)
    policy_intent = intent.get("name") in ("refund_policy", "shipping_policy")
    return bool(state.get("context_block")) and (
        bool(state["agent"].guardrails.get("require_citations")) or policy_intent
    )
