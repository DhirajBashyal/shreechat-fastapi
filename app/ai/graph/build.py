"""The compiled agent graph (docs/ai/LANGGRAPH_WORKFLOW.md §1, §3)."""

import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from opentelemetry import trace

from app.ai.graph import edges, nodes
from app.ai.graph.state import AgentState
from app.ai.providers import is_transient_llm_error
from app.core.logger import get_logger

logger = get_logger()
tracer = trace.get_tracer("app.ai")

NodeFn = Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]


def traced(name: str, fn: NodeFn) -> Any:
    """One span per node (ai.node.<name>) with run id and token counts; a
    no-op until an OpenTelemetry SDK/exporter is configured. Also logs the
    node duration at debug level."""

    async def node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        started = time.monotonic()
        with tracer.start_as_current_span(f"ai.node.{name}") as span:
            span.set_attribute("ai.run_id", str(state.get("run_id")))
            result = await fn(state, config)
            tokens_in = sum(
                int(u.get("input_tokens", 0)) for u in result.get("usage", [])
            )
            tokens_out = sum(
                int(u.get("output_tokens", 0)) for u in result.get("usage", [])
            )
            span.set_attribute("ai.tokens.input", tokens_in)
            span.set_attribute("ai.tokens.output", tokens_out)
        logger.debug(
            "AI node finished",
            extra={
                "node": name,
                "run_id": str(state.get("run_id")),
                "ms": int((time.monotonic() - started) * 1000),
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
            },
        )
        return result

    node.__name__ = name
    return node


LLM_RETRY = RetryPolicy(
    max_attempts=3,
    initial_interval=1.0,
    backoff_factor=2.0,
    jitter=True,
    retry_on=is_transient_llm_error,
)


def build_agent_graph() -> Any:
    g = StateGraph(AgentState)
    g.add_node("input_validation", traced("input_validation", nodes.input_validation))
    g.add_node("tenant_context", traced("tenant_context", nodes.tenant_context))
    g.add_node(
        "conversation_context",
        traced("conversation_context", nodes.conversation_context),
    )
    g.add_node(
        "customer_identification",
        traced("customer_identification", nodes.customer_identification),
    )
    g.add_node(
        "intent_classification",
        traced("intent_classification", nodes.intent_classification),
        retry_policy=LLM_RETRY,
    )
    g.add_node(
        "knowledge_retrieval", traced("knowledge_retrieval", nodes.knowledge_retrieval)
    )
    g.add_node(
        "policy_document_search",
        traced("policy_document_search", nodes.policy_document_search),
    )
    g.add_node("product_search", traced("product_search", nodes.product_search))
    g.add_node("order_lookup", traced("order_lookup", nodes.order_lookup))
    g.add_node(
        "customer_information",
        traced("customer_information", nodes.customer_information),
    )
    g.add_node("rag", traced("rag", nodes.rag))
    g.add_node(
        "response_generation",
        traced("response_generation", nodes.response_generation),
        retry_policy=LLM_RETRY,
    )
    g.add_node(
        "guardrails_validation",
        traced("guardrails_validation", nodes.guardrails_validation),
    )
    g.add_node("human_handoff", traced("human_handoff", nodes.human_handoff))
    g.add_node(
        "response_delivery", traced("response_delivery", nodes.response_delivery)
    )
    g.add_node(
        "memory_persistence", traced("memory_persistence", nodes.memory_persistence)
    )

    g.add_edge(START, "input_validation")
    g.add_conditional_edges(
        "input_validation",
        edges.after_validation,
        {"continue": "tenant_context", "discard": END},
    )
    g.add_conditional_edges(
        "tenant_context",
        edges.after_tenant_context,
        {"continue": "conversation_context", "handoff": "human_handoff"},
    )
    g.add_edge("conversation_context", "customer_identification")
    g.add_edge("customer_identification", "intent_classification")
    g.add_conditional_edges(
        "intent_classification",
        edges.route_after_intent,
        ["human_handoff", "response_generation", *edges.TOOL_NODES],
    )
    for tool_node in edges.TOOL_NODES:
        g.add_edge(
            tool_node, "rag"
        )  # rag waits for every branch scheduled in the same step
    g.add_conditional_edges(
        "rag",
        edges.after_rag,
        {"generate": "response_generation", "handoff": "human_handoff"},
    )
    g.add_edge("response_generation", "guardrails_validation")
    g.add_conditional_edges(
        "guardrails_validation",
        edges.after_guardrails,
        {
            "deliver": "response_delivery",
            "regenerate": "response_generation",
            "handoff": "human_handoff",
        },
    )
    g.add_edge("human_handoff", "response_delivery")
    g.add_conditional_edges(
        "response_delivery",
        edges.after_delivery,
        {"persist": "memory_persistence", "discard": END},
    )
    g.add_edge("memory_persistence", END)
    return g.compile()  # checkpointer in R2 (MEMORY_MANAGEMENT.md §5)


@lru_cache(maxsize=1)
def graph() -> Any:
    """Compiled once per process."""
    return build_agent_graph()
