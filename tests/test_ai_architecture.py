"""docs/ai/AI_ARCHITECTURE.md: trigger queue, working hours, redaction, model
allow-list, token budget, moderation, observability, evaluation harness."""

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.ai import prompts, queue, runner
from app.ai.providers import model_allowed
from app.ai.working_hours import is_open
from app.core import metrics
from app.core.config import settings
from tests.conftest import API, bearer, run_sql, signup
from tests.test_ai_agent import (
    FakeLLM,
    _agent,
    _conversation,
    _incoming,
    _kb,
    _on_message,
    _play,
    _ws,
)

# --- Unit ---------------------------------------------------------------------------


def test_working_hours() -> None:
    hours = [
        {"day": 1, "open": "09:00", "close": "17:00"},
        {"day": 5, "open": "22:00", "close": "06:00"},
    ]
    monday_10 = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)  # a Monday
    assert is_open(hours, "UTC", monday_10)
    assert not is_open(hours, "UTC", monday_10.replace(hour=18))
    assert not is_open(
        hours, "UTC", datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    )  # Tuesday: no entry
    assert is_open(
        hours, "UTC", datetime(2026, 11, 21, 3, 0, tzinfo=UTC)
    )  # Saturday 03:00, Friday's night shift
    # Timezones: 10:00 UTC is 15:45 in Kathmandu (open), 19:45 in Tokyo (closed).
    assert is_open(hours, "Asia/Kathmandu", monday_10)
    assert not is_open(hours, "Asia/Tokyo", monday_10)
    assert not is_open(
        [{"day": 1, "open": "09:00", "close": "17:00", "closed": True}],
        "UTC",
        monday_10,
    )


def test_redaction_keeps_dates_and_order_numbers() -> None:
    text = "Card 4111 1111 1111 1111, IBAN DE89 3704 0044 0532 0130 00, SSN 123-45-6789, order #12345678 on 2026-11-13"
    out = prompts.redact_pii(text)
    assert "4111" not in out and "DE89" not in out and "123-45-6789" not in out
    assert "#12345678" in out and "2026-11-13" in out
    assert (
        prompts.redact_pii("ref 1234 5678 9012 3456") == "ref 1234 5678 9012 3456"
    )  # not Luhn-valid


def test_model_allow_list_honours_deprecation(monkeypatch: pytest.MonkeyPatch) -> None:
    assert model_allowed("openai", "gpt-4o-mini")
    assert model_allowed("anthropic", "claude-sonnet-5-5")
    assert not model_allowed("openai", "gpt-2")
    assert not model_allowed(
        "openai_compatible", "llama3.1:8b"
    )  # not a supported provider
    from app.ai import providers

    info = providers.model_allow_list()[("openai", "gpt-4o")]
    monkeypatch.setitem(
        providers.model_allow_list(),
        ("openai", "gpt-4o"),
        providers.ModelInfo(**{**info.__dict__, "deprecated_at": date(2026, 1, 1)}),
    )
    assert not model_allowed("openai", "gpt-4o", today=date(2026, 6, 1))
    assert model_allowed("openai", "gpt-4o", today=date(2025, 6, 1))


# --- Queue: debounce, one run per conversation, per-org limit --------------------------


def _queue_run(coro_factory: Any) -> Any:
    async def go() -> Any:
        redis = Redis.from_url(settings.REDIS_URL)
        engine = create_async_engine(settings.DB_URL, poolclass=NullPool)
        try:
            await redis.delete(queue.DUE)
            return await coro_factory(
                redis, async_sessionmaker(engine, expire_on_commit=False)
            )
        finally:
            await redis.delete(queue.DUE)
            await redis.aclose()
            await engine.dispose()

    return asyncio.run(go())


def test_queue_debounces_and_runs_the_last_message_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = signup(client)
    c = _conversation(owner)
    first, last = _incoming(c, "hi"), _incoming(c, "where is my order?")
    calls: list[uuid.UUID] = []

    async def handler(session: AsyncSession, message_id: uuid.UUID) -> str:
        calls.append(message_id)
        return "ok"

    async def scenario(redis: Redis, factory: Any) -> tuple[int, int, int]:
        conv = uuid.UUID(c["conv"])
        await queue.enqueue_incoming(redis, conv, uuid.UUID(first), now=1000.0)
        await queue.enqueue_incoming(
            redis, conv, uuid.UUID(last), now=1001.0
        )  # last message wins
        early = await queue.process_due(
            redis, factory, handler, now=1002.0
        )  # not due yet (1003.5)
        due = await queue.process_due(redis, factory, handler, now=1004.0)
        again = await queue.process_due(redis, factory, handler, now=1010.0)
        return early, due, again

    assert _queue_run(scenario) == (0, 1, 0)
    assert calls == [uuid.UUID(last)]


def test_queue_respects_conversation_lock_and_org_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = signup(client)
    c = _conversation(owner)
    mid = _incoming(c, "hello")
    calls: list[uuid.UUID] = []

    async def handler(session: AsyncSession, message_id: uuid.UUID) -> str:
        calls.append(message_id)
        return "ok"

    async def scenario(redis: Redis, factory: Any) -> list[int]:
        conv = uuid.UUID(c["conv"])
        out = []
        # A run is already active for this conversation → re-scheduled, not run.
        await redis.set(f"ai:lock:{conv}", "1", ex=60)
        await queue.enqueue_incoming(redis, conv, uuid.UUID(mid), now=1000.0)
        out.append(await queue.process_due(redis, factory, handler, now=1003.0))
        out.append(int(await redis.zscore(queue.DUE, str(conv)) or 0))
        await redis.delete(f"ai:lock:{conv}")
        # Organization at its limit → re-scheduled.
        monkeypatch.setattr(settings, "AI_MAX_CONCURRENT_RUNS_PER_ORG", 0)
        out.append(await queue.process_due(redis, factory, handler, now=1005.0))
        monkeypatch.setattr(settings, "AI_MAX_CONCURRENT_RUNS_PER_ORG", 5)
        out.append(await queue.process_due(redis, factory, handler, now=1010.0))
        out.append(int(await redis.get(f"ai:running:{c['org']}") or 0))
        return out

    started_locked, rescheduled_at, started_limited, started_ok, running_after = (
        _queue_run(scenario)
    )
    assert (
        started_locked,
        rescheduled_at,
        started_limited,
        started_ok,
        running_after,
    ) == (0, 1004, 0, 1, 0)
    assert calls == [uuid.UUID(mid)]


# --- Graph behaviour ----------------------------------------------------------------


class RecordingLLM(FakeLLM):
    def __init__(self, flagged: list[str] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.flagged = flagged
        self.sent: list[str] = []
        self.systems: list[str] = []

    async def structured(
        self, schema: Any, system: str, messages: list[Any], **kw: Any
    ) -> Any:
        self.sent.extend(text for _, text in messages)
        self.systems.append(system)
        return await super().structured(schema, system, messages, **kw)

    async def moderate(self, text: str) -> list[str] | None:
        return self.flagged


def test_moderation_flag_blocks_and_hands_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_embeddings: Any
) -> None:
    llm = RecordingLLM(flagged=["violence"])
    monkeypatch.setattr(runner, "build_llm", lambda credential: llm)
    owner = signup(client)
    agent = _agent(client, owner)
    _kb(client, owner, agent["id"])
    out = _play(client, owner, agent["id"], "refunds processed")
    assert out["outcome"] == "handoff" and out["handoff_reason"] == "guardrail:unsafe"


def test_pii_is_redacted_before_reaching_the_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = RecordingLLM()
    monkeypatch.setattr(runner, "build_llm", lambda credential: llm)
    owner = signup(client)
    agent = _agent(
        client,
        owner,
        guardrails={"pii_redaction": True},
        handoff_policy={"on_no_answer": "answer"},
    )
    _play(client, owner, agent["id"], "refund to card 4111 1111 1111 1111 please")
    assert llm.sent and not any("4111" in t for t in llm.sent)
    assert any("[card number]" in t for t in llm.sent)


def test_no_answer_instruction_when_knowledge_is_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = RecordingLLM()
    monkeypatch.setattr(runner, "build_llm", lambda credential: llm)
    owner = signup(client)
    agent = _agent(client, owner, handoff_policy={"on_no_answer": "answer"})
    out = _play(client, owner, agent["id"], "what is your refund policy")
    assert out["outcome"] == "reply"
    assert prompts.NO_ANSWER_RULE in llm.systems[-1]


def test_token_budget_hands_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_embeddings: Any
) -> None:
    monkeypatch.setattr(runner, "build_llm", lambda credential: FakeLLM())
    monkeypatch.setattr(
        settings, "AI_RUN_TOKEN_BUDGET", 10
    )  # the classifier alone uses 15
    owner = signup(client)
    agent = _agent(client, owner)
    out = _play(client, owner, agent["id"], "refunds processed")
    assert out["outcome"] == "handoff" and out["handoff_reason"] == "token_budget"


def test_outside_working_hours_hands_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "build_llm", lambda credential: FakeLLM())
    owner = signup(client)
    agent = _agent(client, owner, handoff_policy={"outside_hours": "handoff"})
    c = _conversation(owner)
    client.put(
        f"{API}/workspaces/{c['ws']}/ai-agents/{agent['id']}/inboxes",
        json={"inboxes": [{"inbox_id": c["inbox"]}]},
        headers=bearer(owner),
    )
    run_sql(
        "UPDATE inboxes SET working_hours_enabled = true, working_hours = '[]'::jsonb WHERE id = :i",
        i=c["inbox"],
    )
    result = _on_message(_incoming(c, "refund please"))
    assert result.outcome == "handoff" and result.handoff_reason == "outside_hours"


# --- Observability --------------------------------------------------------------------


def test_metrics_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.AI_RUNS.inc(status="replied")
    body = client.get("/metrics").text
    for name in (
        "ai_runs_total",
        "ai_handoff_total",
        "ai_run_latency_seconds_bucket",
        "ai_tokens_total",
    ):
        assert name in body
    monkeypatch.setattr(settings, "METRICS_TOKEN", "s3cret")
    assert client.get("/metrics").status_code == 401
    assert (
        client.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code
        == 200
    )


def test_eval_harness_reports_recall(
    client: TestClient, fake_embeddings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.db import sessionmanager
    from app.jobs.ai_eval import evaluate

    monkeypatch.setattr(runner, "build_llm", lambda credential: FakeLLM())
    owner = signup(client)
    agent = _agent(client, owner)
    _kb(client, owner, agent["id"])
    [(doc_id,)] = run_sql(
        "SELECT id::text FROM kb_documents WHERE workspace_id = :w", w=_ws(owner)
    )
    cases: list[dict[str, Any]] = [
        {
            "question": "refunds processed business days",
            "expected_document_ids": [doc_id],
            "must_include": ["5-7 business days"],
        },
        {"question": "do you sell boats", "expect_handoff": True},
    ]
    # evaluate() builds its own engine on its own event loop; give the app's back after.
    saved = sessionmanager.engine, sessionmanager.session_factory
    try:
        report = asyncio.run(
            evaluate(uuid.UUID(_ws(owner)), cases, uuid.UUID(agent["id"]))
        )
    finally:
        sessionmanager.engine, sessionmanager.session_factory = saved
    s = report["summary"]
    assert s["cases"] == 2 and s["recall_at_8"] == 1.0
    assert report["results"][0]["passed"] is True
    assert s["refusal_correctness"] == 1.0  # the unanswerable question was handed off


def test_ai_models_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = signup(client)
    assert client.get(f"{API}/ai-models").status_code == 401
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    body = client.get(f"{API}/ai-models", headers=bearer(owner)).json()
    assert body["platform_configured"] is False
    assert {m["provider"] for m in body["models"]} == {"openai", "gemini", "anthropic"}
    assert all(m["selectable"] for m in body["models"])
