"""What an AI agent may do, and how its configuration is validated.

docs/ai/AI_ROLES_AND_PERMISSIONS.md §2.3 (escalation rules), §3.3 (actions),
§3.4 (tool policies); docs/ai/HUMAN_HANDOFF.md §3 (handoff policy).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.exceptions import APIError, PermissionDeniedError, ValidationFailedError

# --- Actions ------------------------------------------------------------------

ACTIONS = (
    "reply",
    "handoff",
    "add_label",
    "set_priority",
    "resolve",
    "snooze",
    "update_contact",
    "add_private_note",
)
DEFAULT_ACTIONS = ("reply", "handoff")

# --- Tools --------------------------------------------------------------------

Verification = Literal["none", "channel", "verified"]
LEVELS: dict[str, int] = {"none": 0, "channel": 1, "verified": 2}


class ToolPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    effect: Literal["read", "write"]
    min_verification: Verification
    data_scope: Literal["tenant_public", "own_contact"]
    requires_integration: str | None = None
    requires_approval: bool = False
    max_calls_per_run: int = 3
    timeout_seconds: float = 5.0


TOOL_POLICIES: dict[str, ToolPolicy] = {
    p.name: p
    for p in (
        ToolPolicy(
            name="knowledge_search",
            effect="read",
            min_verification="none",
            data_scope="tenant_public",
        ),
        ToolPolicy(
            name="policy_search",
            effect="read",
            min_verification="none",
            data_scope="tenant_public",
        ),
        ToolPolicy(
            name="product_search",
            effect="read",
            min_verification="none",
            data_scope="tenant_public",
            requires_integration="commerce",
        ),
        ToolPolicy(
            name="customer_info",
            effect="read",
            min_verification="channel",
            data_scope="own_contact",
        ),
        ToolPolicy(
            name="order_lookup",
            effect="read",
            min_verification="verified",
            data_scope="own_contact",
            requires_integration="commerce",
        ),
    )
}
# Settings that aren't a tool but live in tool_settings (docs §3.4 example).
EXTRA_SETTING_KEYS = {"add_label"}


class _ToolOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_verification: Verification | None = None
    max_calls_per_run: int | None = Field(default=None, ge=0, le=10)
    timeout_seconds: float | None = Field(default=None, gt=0, le=30)


class _AddLabelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_labels: list[str] = Field(default_factory=list, max_length=50)


def effective_policy(tool: str, tool_settings: dict[str, Any]) -> ToolPolicy:
    """Platform policy tightened by the tenant's tool_settings (never loosened)."""
    base = TOOL_POLICIES[tool]
    override = _ToolOverride.model_validate(tool_settings.get(tool) or {})
    changes: dict[str, Any] = {}
    if (
        override.min_verification
        and LEVELS[override.min_verification] > LEVELS[base.min_verification]
    ):
        changes["min_verification"] = override.min_verification
    if override.max_calls_per_run is not None:
        changes["max_calls_per_run"] = min(
            override.max_calls_per_run, base.max_calls_per_run
        )
    if override.timeout_seconds is not None:
        changes["timeout_seconds"] = min(override.timeout_seconds, base.timeout_seconds)
    return base.model_copy(update=changes)


# --- Handoff policy and guardrails --------------------------------------------


class HandoffPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_customer_request: bool = True
    on_intents: list[str] = Field(default_factory=lambda: ["complaint"])
    on_low_confidence: float = Field(default=0.55, ge=0, le=1)
    on_no_answer: Literal["handoff", "answer"] = "handoff"
    on_negative_sentiment_streak: int = Field(default=2, ge=0, le=10)
    on_ai_error: Literal["handoff", "ignore"] = "handoff"
    outside_hours: Literal["ai_continues", "handoff"] = "ai_continues"
    assign_team_id: str | None = None
    handoff_message: dict[str, str] = Field(
        default_factory=lambda: {
            "en": "I'm connecting you with a member of our team. They'll reply here shortly."
        }
    )
    post_internal_note: bool = True


class Guardrails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_reply_chars: int = Field(default=1500, ge=100, le=10_000)
    blocked_topics: list[str] = Field(default_factory=list, max_length=50)
    require_citations: bool = False
    pii_redaction: bool = False
    show_sources: bool = False


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="Assistant", max_length=60)
    tone: str = Field(default="friendly and concise", max_length=200)
    language_policy: Literal["match_customer", "fixed"] = "match_customer"
    language: str | None = Field(default=None, max_length=16)


# --- Validation on save (AIAgentService) --------------------------------------


class AgentConfigError(APIError):
    status_code = 422
    code = "validation_error"


def _err(code: str, message: str) -> AgentConfigError:
    return AgentConfigError(message, code=code)


def validate_actions(actions: list[str]) -> list[str]:
    unknown = sorted(set(actions) - set(ACTIONS))
    if unknown:
        raise _err("action_not_supported", f"Unsupported actions: {', '.join(unknown)}")
    if "handoff" not in actions:
        raise _err(
            "handoff_required", "The AI must always be able to hand off to a human"
        )
    return sorted(set(actions), key=ACTIONS.index)


def validate_tools(tools: list[str]) -> list[str]:
    unknown = sorted(set(tools) - set(TOOL_POLICIES))
    if unknown:
        raise _err("tool_not_supported", f"Unknown tools: {', '.join(unknown)}")
    return sorted(set(tools))


def validate_tool_settings(
    tool_settings: dict[str, Any], workspace_labels: set[str]
) -> dict[str, Any]:
    for key, value in tool_settings.items():
        if key == "add_label":
            try:
                labels = _AddLabelSettings.model_validate(value).allowed_labels
            except ValidationError as exc:
                raise _err(
                    "validation_error",
                    f"tool_settings.add_label: {exc.errors()[0]['msg']}",
                ) from None
            missing = sorted(set(labels) - workspace_labels)
            if missing:
                raise _err(
                    "label_not_in_workspace",
                    f"Labels not in this workspace: {', '.join(missing)}",
                )
            continue
        if key not in TOOL_POLICIES:
            raise _err("validation_error", f"Unknown tool in tool_settings: {key}")
        try:
            override = _ToolOverride.model_validate(value)
        except ValidationError as exc:
            raise _err(
                "validation_error", f"tool_settings.{key}: {exc.errors()[0]['msg']}"
            ) from None
        base = TOOL_POLICIES[key]
        if (
            override.min_verification
            and LEVELS[override.min_verification] < LEVELS[base.min_verification]
        ):
            raise _err(
                "policy_cannot_be_relaxed",
                f"{key}.min_verification can't be lower than '{base.min_verification}'",
            )
        if (
            override.max_calls_per_run is not None
            and override.max_calls_per_run > base.max_calls_per_run
        ):
            raise _err(
                "policy_cannot_be_relaxed",
                f"{key}.max_calls_per_run can't exceed {base.max_calls_per_run}",
            )
        if (
            override.timeout_seconds is not None
            and override.timeout_seconds > base.timeout_seconds
        ):
            raise _err(
                "policy_cannot_be_relaxed",
                f"{key}.timeout_seconds can't exceed {base.timeout_seconds}",
            )
    return tool_settings


def check_escalation(
    permissions: frozenset[str], *, enabled_tools: list[str], allowed_actions: list[str]
) -> None:
    """No privilege escalation through the AI (docs §2.3.1)."""
    if (
        "customer_info" in enabled_tools
        and "custom_attribute.manage" not in permissions
    ):
        raise PermissionDeniedError("custom_attribute.manage")
    if "update_contact" in allowed_actions and "contact.manage" not in permissions:
        raise PermissionDeniedError("contact.manage")


def parse_model(
    model_cls: type[BaseModel], value: dict[str, Any], field: str
) -> dict[str, Any]:
    try:
        return model_cls.model_validate(value).model_dump()
    except ValidationError as exc:
        e = exc.errors()[0]
        loc = ".".join(str(p) for p in e["loc"])
        raise ValidationFailedError(f"{field}.{loc}: {e['msg']}") from None
