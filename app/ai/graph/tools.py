"""Tool executor (docs/ai/AI_ROLES_AND_PERMISSIONS.md §3.5).

Every tool call passes through ``execute``: enabled? call limit? customer
verification level? approval? own data only? Every call, allowed or not, is
returned as a ``tool_calls`` entry and stored in ai_tool_calls by the runner.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.ai.graph.state import AgentState, ToolResult
from app.ai.policy import LEVELS, effective_policy

ToolFn = Callable[[], Awaitable[ToolResult]]


def _result(
    tool: str, *, ok: bool, error: str | None = None, text: str = ""
) -> ToolResult:
    return ToolResult(
        tool=tool,
        ok=ok,
        data=None,
        text=text,
        sources=[],
        error=error,
        sufficient=False,
    )


def _call(
    tool: str,
    args: dict[str, Any],
    decision: str,
    *,
    reason: str | None = None,
    ok: bool = False,
    excerpt: str | None = None,
    latency_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "tool_name": tool,
        "arguments": args,
        "policy_decision": decision,
        "denial_reason": reason,
        "succeeded": ok,
        "result_excerpt": excerpt,
        "latency_ms": latency_ms,
    }


async def execute(
    state: AgentState, tool: str, args: dict[str, Any], run: ToolFn
) -> dict[str, Any]:
    """Returns the node's state update: {"tool_results": [...], "tool_calls": [...]}."""
    agent = state["agent"]
    policy = effective_policy(tool, agent.tool_settings)
    previous = sum(1 for c in state.get("tool_calls", []) if c["tool_name"] == tool)

    decision: tuple[str, str | None]
    if tool not in agent.enabled_tools:
        decision = ("denied", "tool_not_enabled")
    elif previous >= policy.max_calls_per_run:
        decision = ("denied", "call_limit_reached")
    elif LEVELS[state["customer"].verification_level] < LEVELS[policy.min_verification]:
        decision = ("needs_verification", f"requires_{policy.min_verification}")
    elif policy.requires_approval:
        decision = ("needs_approval", "approval_required")
    else:
        decision = ("allowed", None)

    if decision[0] != "allowed":
        error = (
            "verification_required"
            if decision[0] == "needs_verification"
            else decision[1]
        )
        return {
            "tool_results": [_result(tool, ok=False, error=error)],
            "tool_calls": [_call(tool, args, decision[0], reason=decision[1])],
        }

    started = time.monotonic()
    try:
        result = await run()
    except (
        Exception
    ) as exc:  # tool failure is data, not a crash (LANGGRAPH_WORKFLOW.md §6)
        latency = int((time.monotonic() - started) * 1000)
        return {
            "tool_results": [_result(tool, ok=False, error=type(exc).__name__)],
            "tool_calls": [
                _call(
                    tool,
                    args,
                    "allowed",
                    ok=False,
                    excerpt=str(exc)[:200],
                    latency_ms=latency,
                )
            ],
            "errors": [{"node": tool, "error": str(exc)[:300]}],
        }
    latency = int((time.monotonic() - started) * 1000)
    return {
        "tool_results": [result],
        "tool_calls": [
            _call(
                tool,
                args,
                "allowed",
                ok=result["ok"],
                excerpt=result["text"][:300],
                latency_ms=latency,
            )
        ],
    }
