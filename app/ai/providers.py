"""LLM providers (docs/ai/AI_ARCHITECTURE.md §4).

Graph nodes never talk to a vendor SDK. They call ``LLM.structured(schema,
system, messages)`` and get a validated Pydantic object back, plus token
usage. ``LangChainLLM`` implements it with LangChain chat models; tests use a
fake. ``build_llm()`` picks the tenant's credential (bring your own key) when
the agent has one, otherwise the platform settings (AI_PROVIDER, AI_API_KEY).
"""

import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

import yaml  # type: ignore[import-untyped]
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from app.core.config import settings
from app.core.crypto import decrypt_secret
from app.models import AiProviderCredential

T = TypeVar("T", bound=BaseModel)

if not settings.AI_TRACING:
    # LangChain auto-enables LangSmith when these are set in the environment;
    # traces contain customer content, so they stay off unless AI_TRACING=true.
    for _var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        os.environ[_var] = "false"
Role = Literal["user", "assistant"]

SUPPORTED_PROVIDERS = ("openai", "gemini", "anthropic")
MODELS_FILE = Path(__file__).resolve().parents[2] / "config" / "llm_models.yaml"


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str
    supports_tools: bool
    supports_structured_output: bool
    context_window: int
    cost_per_1k_input: float | None
    cost_per_1k_output: float | None
    deprecated_at: date | None


@lru_cache(maxsize=1)
def model_allow_list() -> dict[tuple[str, str], ModelInfo]:
    """config/llm_models.yaml, loaded once per process."""
    raw = yaml.safe_load(MODELS_FILE.read_text()) or {}
    out: dict[tuple[str, str], ModelInfo] = {}
    for m in raw.get("models", []):
        dep = m.get("deprecated_at")
        info = ModelInfo(
            provider=m["provider"],
            model=m["model"],
            supports_tools=bool(m.get("supports_tools")),
            supports_structured_output=bool(m.get("supports_structured_output")),
            context_window=int(m.get("context_window") or 0),
            cost_per_1k_input=m.get("cost_per_1k_input"),
            cost_per_1k_output=m.get("cost_per_1k_output"),
            deprecated_at=dep
            if isinstance(dep, date)
            else (date.fromisoformat(dep) if dep else None),
        )
        out[(info.provider, info.model)] = info
    return out


def model_allowed(provider: str, model: str, *, today: date | None = None) -> bool:
    """Listed, supports structured output (every node needs it), not past its
    deprecation date."""
    info = model_allow_list().get((provider, model))
    if info is None or not info.supports_structured_output:
        return False
    return info.deprecated_at is None or info.deprecated_at > (today or date.today())


class ProviderNotConfiguredError(Exception):
    pass


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLM(Protocol):
    provider: str

    async def moderate(self, text: str) -> list[str] | None: ...

    async def structured(
        self,
        schema: type[T],
        system: str,
        messages: list[tuple[Role, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> tuple[T, Usage]: ...


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    api_key: str


class LangChainLLM:
    def __init__(self, config: ProviderConfig) -> None:
        if config.provider not in SUPPORTED_PROVIDERS:
            raise ProviderNotConfiguredError(
                f"Provider '{config.provider}' is not supported"
            )
        if not config.api_key:
            raise ProviderNotConfiguredError(
                "No API key configured for the AI provider"
            )
        self.provider = config.provider
        self._config = config

    def _chat_model(
        self, model: str, temperature: float, max_tokens: int
    ) -> BaseChatModel:
        c = self._config
        if c.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(  # type: ignore[call-arg]
                model_name=model,
                api_key=c.api_key,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens_to_sample=max_tokens,
                timeout=30,
                max_retries=0,
            )
        if c.provider == "gemini":
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(
                model=model,
                google_api_key=c.api_key,
                temperature=temperature,
                max_output_tokens=max_tokens,
                timeout=30,
                max_retries=0,  # retries are the graph's RetryPolicy
            )
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            api_key=c.api_key,  # type: ignore[arg-type]
            temperature=temperature,
            max_completion_tokens=max_tokens,
            timeout=30,
            max_retries=0,
        )

    async def structured(
        self,
        schema: type[T],
        system: str,
        messages: list[tuple[Role, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> tuple[T, Usage]:
        chat = self._chat_model(model, temperature, max_tokens)
        lc_messages: list[BaseMessage] = [SystemMessage(system)]
        for role, text in messages:
            lc_messages.append(
                HumanMessage(text) if role == "user" else AIMessage(text)
            )
        runnable = chat.with_structured_output(schema, include_raw=True)
        out: Any = await runnable.ainvoke(lc_messages)
        if out.get("parsing_error") or out.get("parsed") is None:
            raise StructuredOutputError(str(out.get("parsing_error") or "no output"))
        usage = Usage(model=model)
        meta = getattr(out.get("raw"), "usage_metadata", None) or {}
        usage.input_tokens = int(meta.get("input_tokens", 0))
        usage.output_tokens = int(meta.get("output_tokens", 0))
        parsed = out["parsed"]
        return (
            parsed if isinstance(parsed, schema) else schema.model_validate(parsed)
        ), usage

    async def moderate(self, text: str) -> list[str] | None:
        """Flagged moderation categories, or None when the provider has no
        moderation endpoint (then guardrails rely on the other checks)."""
        if self.provider != "openai" or not text.strip():
            return None
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self._config.api_key, timeout=10)
        result = await client.moderations.create(
            model="omni-moderation-latest", input=text
        )
        categories = result.results[0].categories.model_dump()
        return sorted(name for name, flagged in categories.items() if flagged)


class StructuredOutputError(Exception):
    """The model's output didn't match the schema."""


def platform_config() -> ProviderConfig:
    return ProviderConfig(
        provider=settings.AI_PROVIDER,
        api_key=settings.AI_API_KEY,
    )


def config_for(credential: AiProviderCredential | None) -> ProviderConfig:
    if credential is None:
        return platform_config()
    return ProviderConfig(
        provider=credential.provider,
        api_key=decrypt_secret(credential.api_key_ciphertext),
    )


def build_llm(credential: AiProviderCredential | None) -> LLM:
    return LangChainLLM(config_for(credential))


def is_transient_llm_error(exc: BaseException) -> bool:
    """429 / 5xx / timeouts are retried by the graph's RetryPolicy."""
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if isinstance(status, int):
        return status == 429 or status >= 500
    name = type(exc).__name__.lower()
    return any(k in name for k in ("timeout", "connection", "ratelimit", "unavailable"))
