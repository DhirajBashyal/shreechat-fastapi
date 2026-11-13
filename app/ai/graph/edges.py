"""Conditional edges (docs/ai/LANGGRAPH_WORKFLOW.md §4): plain Python on structured state."""

from app.ai.graph.guardrails import BLOCKING, FIXABLE
from app.ai.graph.state import AgentState
from app.core.config import settings

TOOL_NODE_FOR_NEED = {
    "knowledge": "knowledge_retrieval",
    "policy": "policy_document_search",
    "product": "product_search",
    "order": "order_lookup",
    "customer": "customer_information",
}
TOOL_NAME_FOR_NODE = {
    "knowledge_retrieval": "knowledge_search",
    "policy_document_search": "policy_search",
    "product_search": "product_search",
    "order_lookup": "order_lookup",
    "customer_information": "customer_info",
}
TOOL_NODES = tuple(TOOL_NAME_FOR_NODE)
DIRECT_INTENTS = ("greeting", "smalltalk", "thanks")


def tokens_used(state: AgentState) -> int:
    return sum(
        int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
        for u in state.get("usage", [])
    )


def over_budget(state: AgentState) -> bool:
    """Per-run token budget (docs/ai/AI_ARCHITECTURE.md §2.5)."""
    return tokens_used(state) > settings.AI_RUN_TOKEN_BUDGET


def intent_handoff_reason(state: AgentState) -> str | None:
    """Why the router sends this run to a human, or None. Shared with the
    human_handoff node so the reason is recorded."""
    if over_budget(state):
        return "token_budget"
    intent = state.get("intent")
    if not intent:
        return None
    agent = state["agent"]
    policy = agent.handoff_policy
    if intent["name"] == "human_request" and policy.get("on_customer_request", True):
        return "customer_request"
    if intent["name"] in policy.get("on_intents", []):
        return f"intent:{intent['name']}"
    if intent["confidence"] < float(policy.get("on_low_confidence", 0.55)):
        return "low_confidence"
    if state["conversation"].ai_turn_count >= agent.max_turns_before_handoff:
        return "turn_limit"
    streak_limit = int(policy.get("on_negative_sentiment_streak", 2))
    if streak_limit and intent["sentiment"] == "negative":
        if (
            int((state.get("facts") or {}).get("negative_streak", 0)) + 1
            >= streak_limit
        ):
            return "negative_sentiment"
    return None


def after_validation(state: AgentState) -> str:
    return "discard" if state.get("outcome") == "discard" else "continue"


def after_tenant_context(state: AgentState) -> str:
    return "handoff" if state.get("handoff_reason") else "continue"


def route_after_intent(state: AgentState) -> list[str] | str:
    if intent_handoff_reason(state):
        return "human_handoff"
    intent, agent = state["intent"], state["agent"]
    if intent["name"] in DIRECT_INTENTS:
        return "response_generation"
    nodes: list[str] = []
    for need in intent["needs"] or ["knowledge"]:
        node = TOOL_NODE_FOR_NEED.get(need)
        if (
            node
            and TOOL_NAME_FOR_NODE[node] in agent.enabled_tools
            and node not in nodes
        ):
            nodes.append(node)
    if not nodes and "knowledge_search" in agent.enabled_tools:
        nodes.append("knowledge_retrieval")  # unknown needs: look it up anyway
    return nodes or "response_generation"  # a list → LangGraph runs them in parallel


def after_rag(state: AgentState) -> str:
    if over_budget(state):
        return "handoff"
    if (
        not state.get("context_sufficient")
        and state["agent"].handoff_policy.get("on_no_answer", "handoff") == "handoff"
    ):
        return "handoff"
    return "generate"  # generation says it doesn't know and offers a human


def after_guardrails(state: AgentState) -> str:
    flags = set(state.get("guardrail_flags", []))
    if flags & BLOCKING or state.get("wants_handoff"):
        return "handoff"
    if flags & FIXABLE:
        if over_budget(state):
            return "handoff"
        return "regenerate" if state.get("guardrail_attempts", 0) < 3 else "handoff"
    return "deliver"


def after_delivery(state: AgentState) -> str:
    return "discard" if state.get("outcome") == "discard" else "persist"
