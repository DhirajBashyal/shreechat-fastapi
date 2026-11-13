"""AI agent: configuration rules, playground, live runs, handoff, activity.

docs/ai/*. The LLM is a scripted fake (no API key needed) and embeddings use
the fake from test_knowledge.
"""

import asyncio
import re
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.ai import runner
from app.ai.graph.state import (
    GenerationOutput,
    GroundingOutput,
    IntentOutput,
    SummaryOutput,
)
from app.ai.providers import Usage
from app.core.config import settings
from tests.conftest import API, bearer, run_sql, signup, unique_email
from tests.test_knowledge import POLICY

# --- Fake LLM ------------------------------------------------------------------------


class FakeLLM:
    provider = "fake"

    def __init__(
        self, reply: Callable[[str, str], str] | None = None, fail: bool = False
    ) -> None:
        self.reply = reply or (
            lambda text, system: "Refunds take 5-7 business days [1]."
        )
        self.fail = fail
        self.calls: list[str] = []

    async def structured(
        self, schema: type[BaseModel], system: str, messages: list[Any], **kw: Any
    ) -> tuple[Any, Usage]:
        self.calls.append(schema.__name__)
        if self.fail:
            raise RuntimeError("provider down")
        text = messages[-1][1].lower() if messages else ""
        usage = Usage(model=kw.get("model", "fake"), input_tokens=10, output_tokens=5)
        if schema is IntentOutput:
            if "human" in text or "person" in text:
                return IntentOutput(
                    name="human_request",
                    confidence=0.95,
                    language="en",
                    sentiment="neutral",
                    needs=[],
                ), usage
            if re.search(r"\bhi\b", text):
                return IntentOutput(
                    name="greeting",
                    confidence=0.95,
                    language="en",
                    sentiment="positive",
                    needs=[],
                ), usage
            if "account" in text:
                return IntentOutput(
                    name="account_help",
                    confidence=0.9,
                    language="en",
                    sentiment="neutral",
                    needs=["customer"],
                ), usage
            return IntentOutput(
                name="refund_policy",
                confidence=0.9,
                language="en",
                sentiment="neutral",
                needs=["knowledge"],
                search_query=text,
            ), usage
        if schema is GenerationOutput:
            return GenerationOutput(
                reply=self.reply(text, system), used_sources=[1]
            ), usage
        if schema is GroundingOutput:
            return GroundingOutput(unsupported_claims=[]), usage
        if schema is SummaryOutput:
            return SummaryOutput(summary="Customer asked about refunds."), usage
        raise AssertionError(schema)


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    llm = FakeLLM()
    monkeypatch.setattr(runner, "build_llm", lambda credential: llm)
    return llm


# --- Helpers -------------------------------------------------------------------------


def _ws(t: dict[str, Any]) -> str:
    return str(t["memberships"][0]["workspaces"][0]["id"])


def _org(t: dict[str, Any]) -> str:
    return str(t["memberships"][0]["organization_id"])


def _agent(client: TestClient, owner: dict[str, Any], **body: Any) -> dict[str, Any]:
    r = client.post(
        f"{API}/workspaces/{_ws(owner)}/ai-agents",
        json={"name": "Help bot", "chat_model": "gpt-4o-mini", **body},
        headers=bearer(owner),
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _kb(client: TestClient, owner: dict[str, Any], agent_id: str) -> str:
    ws = _ws(owner)
    kb = client.post(
        f"{API}/workspaces/{ws}/knowledge-bases",
        json={"name": "Help"},
        headers=bearer(owner),
    ).json()
    r = client.post(
        f"{API}/workspaces/{ws}/knowledge-bases/{kb['id']}/documents",
        json={
            "source_type": "text",
            "title": "Refund policy",
            "text": POLICY,
            "category": "policy",
        },
        headers=bearer(owner),
    )
    assert r.json()["status"] == "ready", r.text
    r = client.put(
        f"{API}/workspaces/{ws}/ai-agents/{agent_id}/knowledge-bases",
        json={"knowledge_base_ids": [kb["id"]]},
        headers=bearer(owner),
    )
    assert r.status_code == 200, r.text
    return str(kb["id"])


def _join(
    client: TestClient,
    owner: dict[str, Any],
    sent: list[dict[str, str]],
    role: str = "agent",
) -> dict[str, Any]:
    email = unique_email(role)
    client.post(
        f"{API}/organizations/{_org(owner)}/invitations",
        json={"email": email, "role": role},
        headers=bearer(owner),
    )
    token = re.search(r"token=([\w\-]+)", sent[-1]["body"])
    assert token
    member = signup(client, email=email, invitation_token=token.group(1))
    client.patch(
        f"{API}/organizations/{_org(owner)}/members/{member['memberships'][0]['membership_id']}",
        json={"workspace_ids": [_ws(owner)]},
        headers=bearer(owner),
    )
    return member


def _conversation(
    owner: dict[str, Any], *, provider: str = "web_widget", verified: bool = False
) -> dict[str, str]:
    """Inbox + contact + identity + conversation with the AI active."""
    org, ws = _org(owner), _ws(owner)
    ids = {k: str(uuid.uuid4()) for k in ("inbox", "contact", "identity", "conv")}
    run_sql(
        "INSERT INTO inboxes (id, organization_id, workspace_id, name, provider) VALUES (:inbox, :org, :ws, 'Website', CAST(:p AS channel_provider))",
        org=org,
        ws=ws,
        p=provider,
        **ids,
    )
    run_sql(
        "INSERT INTO contacts (id, organization_id, workspace_id, name, email) VALUES (:contact, :org, :ws, 'Casey Customer', 'casey@example.com')",
        org=org,
        ws=ws,
        **ids,
    )
    run_sql(
        "INSERT INTO contact_identities (id, organization_id, workspace_id, contact_id, inbox_id, source_id, identity_verified) "
        "VALUES (:identity, :org, :ws, :contact, :inbox, 'v1', :verified)",
        org=org,
        ws=ws,
        verified=verified,
        **{k: ids[k] for k in ("identity", "contact", "inbox")},
    )
    run_sql(
        "INSERT INTO conversations (id, organization_id, workspace_id, inbox_id, contact_id, contact_identity_id, display_number, status, ai_active) "
        "VALUES (:conv, :org, :ws, :inbox, :contact, :identity, 1, 'pending', true)",
        org=org,
        ws=ws,
        **ids,
    )
    return ids | {"org": org, "ws": ws}


def _incoming(c: dict[str, str], text: str) -> str:
    mid = str(uuid.uuid4())
    run_sql(
        "INSERT INTO messages (id, organization_id, workspace_id, inbox_id, conversation_id, direction, content, sender_kind, sender_contact_id) "
        "VALUES (:id, :org, :ws, :inbox, :conv, 'incoming', :text, 'contact', :contact)",
        id=mid,
        text=text,
        **{k: c[k] for k in ("org", "ws", "inbox", "conv", "contact")},
    )
    return mid


def _on_message(message_id: str) -> Any:
    from app.ai.handlers import on_incoming_message

    async def go() -> Any:
        engine = create_async_engine(settings.DB_URL, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                return await on_incoming_message(session, uuid.UUID(message_id))
        finally:
            await engine.dispose()

    return asyncio.run(go())


# --- Configuration rules (AI_ROLES_AND_PERMISSIONS.md §2–3) ----------------------------


def test_agent_defaults_and_prompt_visibility(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    agent = _agent(client, owner, system_prompt="Secret business rules")
    assert agent["allowed_actions"] == ["reply", "handoff"]
    assert agent["enabled_tools"] == ["knowledge_search"]
    assert agent["system_prompt"] == "Secret business rules"
    assert agent["handoff_policy"]["on_customer_request"] is True

    member = _join(client, owner, sent_emails)
    seen = client.get(
        f"{API}/workspaces/{_ws(owner)}/ai-agents", headers=bearer(member)
    ).json()
    assert seen[0]["name"] == "Help bot" and seen[0]["system_prompt"] is None
    r = client.post(
        f"{API}/workspaces/{_ws(owner)}/ai-agents",
        json={"name": "x"},
        headers=bearer(member),
    )
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"allowed_actions": ["reply"]}, "handoff_required"),
        (
            {"allowed_actions": ["reply", "handoff", "issue_refund"]},
            "action_not_supported",
        ),
        ({"enabled_tools": ["web_browse"]}, "tool_not_supported"),
        (
            {"tool_settings": {"customer_info": {"min_verification": "none"}}},
            "policy_cannot_be_relaxed",
        ),
        (
            {"tool_settings": {"order_lookup": {"max_calls_per_run": 9}}},
            "policy_cannot_be_relaxed",
        ),
        (
            {"tool_settings": {"add_label": {"allowed_labels": ["nope"]}}},
            "label_not_in_workspace",
        ),
        ({"chat_model": "gpt-2"}, "model_not_allowed"),
    ],
)
def test_invalid_agent_config_is_rejected(
    client: TestClient, body: dict[str, Any], code: str
) -> None:
    owner = signup(client)
    agent = _agent(client, owner)
    r = client.patch(
        f"{API}/workspaces/{_ws(owner)}/ai-agents/{agent['id']}",
        json=body,
        headers=bearer(owner),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code


def test_no_privilege_escalation_through_the_ai(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    agent = _agent(client, owner)
    member = _join(client, owner, sent_emails)
    [(role_id,)] = run_sql(
        "INSERT INTO roles (organization_id, name) VALUES (:o, 'AI editor') RETURNING id",
        o=_org(owner),
    )
    run_sql(
        "INSERT INTO role_permissions (organization_id, role_id, permission_code) VALUES (:o, :r, 'ai.agent.manage')",
        o=_org(owner),
        r=role_id,
    )
    run_sql(
        "UPDATE organization_memberships SET custom_role_id = :r WHERE id = :m",
        r=role_id,
        m=member["memberships"][0]["membership_id"],
    )
    url = f"{API}/workspaces/{_ws(owner)}/ai-agents/{agent['id']}"
    assert (
        client.patch(
            url, json={"system_prompt": "ok"}, headers=bearer(member)
        ).status_code
        == 200
    )
    r = client.patch(
        url,
        json={"enabled_tools": ["knowledge_search", "customer_info"]},
        headers=bearer(member),
    )
    assert (
        r.status_code == 403
        and r.json()["error"]["permission"] == "custom_attribute.manage"
    )
    r = client.patch(
        url,
        json={"allowed_actions": ["reply", "handoff", "update_contact"]},
        headers=bearer(member),
    )
    assert r.status_code == 403 and r.json()["error"]["permission"] == "contact.manage"


def test_config_changes_are_audited_with_old_and_new_values(client: TestClient) -> None:
    owner = signup(client)
    agent = _agent(client, owner)
    client.patch(
        f"{API}/workspaces/{_ws(owner)}/ai-agents/{agent['id']}",
        json={
            "enabled_tools": ["knowledge_search", "policy_search"],
            "system_prompt": "Be nice",
        },
        headers=bearer(owner),
    )
    rows = dict(
        run_sql(
            "SELECT action, changes FROM audit_logs WHERE resource_id = :a AND action LIKE 'ai_agent.%'",
            a=agent["id"],
        )
    )
    assert rows["ai_agent.tools_changed"]["enabled_tools"] == [
        ["knowledge_search"],
        ["knowledge_search", "policy_search"],
    ]
    assert rows["ai_agent.updated"]["system_prompt"] == ["", "Be nice"]


def test_credentials_need_the_plan_feature_and_are_never_returned(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    url = f"{API}/organizations/{_org(owner)}/ai-credentials"
    body = {"provider": "openai", "label": "Main", "api_key": "sk-test-1234567890abcd"}
    r = client.post(url, json=body, headers=bearer(owner))
    assert r.status_code == 402 and r.json()["error"]["code"] == "feature_not_in_plan"
    run_sql(
        "UPDATE organizations SET feature_flags = ARRAY['byo_llm_keys'] WHERE id = :o",
        o=_org(owner),
    )
    r = client.post(url, json=body, headers=bearer(owner))
    assert r.status_code == 201, r.text
    assert r.json()["api_key_last4"] == "abcd" and "api_key" not in r.json()
    [(cipher,)] = run_sql(
        "SELECT api_key_ciphertext FROM ai_provider_credentials WHERE id = :i",
        i=r.json()["id"],
    )
    assert b"sk-test" not in bytes(cipher)
    member = _join(client, owner, sent_emails)
    assert client.get(url, headers=bearer(member)).status_code == 403


# --- Playground ------------------------------------------------------------------------


def _play(
    client: TestClient, owner: dict[str, Any], agent_id: str, message: str, **extra: Any
) -> dict[str, Any]:
    r = client.post(
        f"{API}/workspaces/{_ws(owner)}/ai-agents/{agent_id}/playground",
        json={"message": message, **extra},
        headers=bearer(owner),
    )
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def test_playground_answers_from_knowledge(
    client: TestClient, fake_llm: FakeLLM, fake_embeddings: Any
) -> None:
    owner = signup(client)
    agent = _agent(client, owner)
    _kb(client, owner, agent["id"])
    out = _play(client, owner, agent["id"], "refunds processed business days")
    assert out["outcome"] == "reply" and "5-7 business days" in out["reply"]
    assert out["intent"]["name"] == "refund_policy"
    assert out["route"] == ["knowledge_search"]
    assert out["citations"][0]["title"] == "Refund policy"
    assert out["tool_calls"][0]["policy_decision"] == "allowed"
    assert "GroundingOutput" in fake_llm.calls  # policy intents are grounding-checked


def test_playground_handoff_paths(
    client: TestClient, fake_llm: FakeLLM, fake_embeddings: Any
) -> None:
    owner = signup(client)
    agent = _agent(client, owner)
    out = _play(client, owner, agent["id"], "I want to talk to a human")
    assert out["outcome"] == "handoff" and out["handoff_reason"] == "customer_request"
    assert out["reply"].startswith("I'm connecting you")
    # No knowledge linked → nothing found → handoff (on_no_answer = handoff).
    out = _play(client, owner, agent["id"], "what is your refund policy")
    assert out["outcome"] == "handoff" and out["handoff_reason"] == "no_answer"
    # Greetings skip the tools.
    out = _play(client, owner, agent["id"], "hi there")
    assert out["outcome"] == "reply" and out["tool_calls"] == []


def test_guardrails_regenerate_then_block(
    client: TestClient, fake_llm: FakeLLM, fake_embeddings: Any
) -> None:
    owner = signup(client)
    agent = _agent(client, owner, guardrails={"max_reply_chars": 100})
    _kb(client, owner, agent["id"])
    drafts = iter(["x" * 300, "Refunds take 5-7 business days."])
    fake_llm.reply = lambda text, system: next(drafts)
    out = _play(client, owner, agent["id"], "refunds processed")
    assert (
        out["outcome"] == "reply" and out["reply"] == "Refunds take 5-7 business days."
    )
    assert fake_llm.calls.count("GenerationOutput") == 2

    fake_llm.reply = lambda text, system: "Honestly, I am a real human, not a bot."
    out = _play(client, owner, agent["id"], "refunds processed")
    assert out["outcome"] == "handoff" and out["handoff_reason"] == "guardrail:unsafe"

    fake_llm.reply = (
        lambda text, system: "Email our other customer bob@other.com for help."
    )
    out = _play(client, owner, agent["id"], "refunds processed")
    assert out["handoff_reason"] == "guardrail:pii_leak"


def test_tool_needs_customer_verification(
    client: TestClient, fake_llm: FakeLLM
) -> None:
    owner = signup(client)
    agent = _agent(
        client,
        owner,
        enabled_tools=["knowledge_search", "customer_info"],
        handoff_policy={"on_no_answer": "answer"},
    )
    out = _play(client, owner, agent["id"], "my account plan")
    [call] = out["tool_calls"]
    assert (
        call["tool_name"] == "customer_info"
        and call["policy_decision"] == "needs_verification"
    )
    out = _play(
        client, owner, agent["id"], "my account plan", customer_verification="channel"
    )
    assert out["tool_calls"][0]["policy_decision"] == "allowed"


def test_playground_without_provider_key_is_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    owner = signup(client)
    agent = _agent(client, owner)
    r = client.post(
        f"{API}/workspaces/{_ws(owner)}/ai-agents/{agent['id']}/playground",
        json={"message": "hello"},
        headers=bearer(owner),
    )
    assert (
        r.status_code == 503
        and r.json()["error"]["code"] == "ai_provider_not_configured"
    )


# --- Live runs on conversations ---------------------------------------------------------


def test_live_reply_runs_once_and_records_activity(
    client: TestClient,
    fake_llm: FakeLLM,
    fake_embeddings: Any,
    sent_emails: list[dict[str, str]],
) -> None:
    owner = signup(client)
    agent = _agent(client, owner)
    _kb(client, owner, agent["id"])
    c = _conversation(owner)
    client.put(
        f"{API}/workspaces/{c['ws']}/ai-agents/{agent['id']}/inboxes",
        json={"inboxes": [{"inbox_id": c["inbox"]}]},
        headers=bearer(owner),
    )
    mid = _incoming(c, "refunds processed business days")

    result = _on_message(mid)
    assert result.outcome == "reply", result
    [(content, kind, client_id)] = run_sql(
        "SELECT content, sender_kind::text, client_message_id FROM messages WHERE conversation_id = :c AND direction = 'outgoing'",
        c=c["conv"],
    )
    assert (
        "5-7 business days" in content
        and kind == "ai_agent"
        and client_id == f"ai:{result.run_id}"
    )
    assert _on_message(mid).duplicate  # same trigger again: no second reply
    [(turns,)] = run_sql(
        "SELECT turn_count FROM ai_conversation_sessions WHERE conversation_id = :c",
        c=c["conv"],
    )
    assert turns == 1
    assert run_sql(
        "SELECT quantity FROM usage_records WHERE source_id = :r", r=str(result.run_id)
    ) == [(1,)]

    runs = client.get(
        f"{API}/workspaces/{c['ws']}/conversations/{c['conv']}/ai-runs",
        headers=bearer(owner),
    ).json()
    assert runs[0]["status"] == "replied" and runs[0]["intent"] == "refund_policy"
    assert (
        runs[0]["sources"][0]["title"] == "Refund policy"
        and "content" in runs[0]["sources"][0]
    )
    assert runs[0]["tool_calls"][0]["tool_name"] == "knowledge_search"
    # A default agent not in this inbox can't see the conversation's AI activity.
    member = _join(client, owner, sent_emails)
    r = client.get(
        f"{API}/workspaces/{c['ws']}/conversations/{c['conv']}/ai-runs",
        headers=bearer(member),
    )
    assert r.status_code == 404
    run_sql(
        "INSERT INTO inbox_members (organization_id, workspace_id, inbox_id, user_id) VALUES (:org, :ws, :inbox, :u)",
        u=member["user"]["id"],
        **{k: c[k] for k in ("org", "ws", "inbox")},
    )
    runs = client.get(
        f"{API}/workspaces/{c['ws']}/conversations/{c['conv']}/ai-runs",
        headers=bearer(member),
    ).json()
    assert (
        "content" not in runs[0]["sources"][0]
    )  # chunk text needs ai.knowledge.manage


def test_live_handoff_and_human_takeover(client: TestClient, fake_llm: FakeLLM) -> None:
    owner = signup(client)
    agent = _agent(client, owner)
    c = _conversation(owner)
    client.put(
        f"{API}/workspaces/{c['ws']}/ai-agents/{agent['id']}/inboxes",
        json={"inboxes": [{"inbox_id": c["inbox"]}]},
        headers=bearer(owner),
    )
    result = _on_message(_incoming(c, "can I talk to a person"))
    assert result.outcome == "handoff" and result.handoff_reason == "customer_request"
    [(ai_active, status)] = run_sql(
        "SELECT ai_active, status::text FROM conversations WHERE id = :c", c=c["conv"]
    )
    assert (ai_active, status) == (False, "open")
    notes = run_sql(
        "SELECT content FROM messages WHERE conversation_id = :c AND is_private",
        c=c["conv"],
    )
    assert notes and "customer asked for a person" in notes[0][0]
    assert run_sql(
        "SELECT reason FROM conversation_assignments WHERE conversation_id = :c",
        c=c["conv"],
    ) == [("ai_handoff",)]
    assert run_sql(
        "SELECT status FROM ai_conversation_sessions WHERE conversation_id = :c",
        c=c["conv"],
    ) == [("handed_off",)]
    # Handed off: the AI stays quiet.
    assert _on_message(_incoming(c, "hello?")) == "human_owns_conversation"

    base = f"{API}/workspaces/{c['ws']}/conversations/{c['conv']}/ai"
    r = client.post(base + "/resume", headers=bearer(owner))
    assert r.json() == {
        "conversation_id": c["conv"],
        "ai_active": True,
        "status": "pending",
    }
    assert _on_message(_incoming(c, "hi again")).outcome == "reply"
    r = client.post(base + "/pause", headers=bearer(owner))
    assert r.json()["ai_active"] is False
    assert _on_message(_incoming(c, "anyone?")) == "human_owns_conversation"


def test_provider_failure_hands_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "build_llm", lambda credential: FakeLLM(fail=True))
    owner = signup(client)
    agent = _agent(client, owner)
    c = _conversation(owner)
    client.put(
        f"{API}/workspaces/{c['ws']}/ai-agents/{agent['id']}/inboxes",
        json={"inboxes": [{"inbox_id": c["inbox"]}]},
        headers=bearer(owner),
    )
    result = _on_message(_incoming(c, "refund please"))
    assert result.outcome == "handoff" and result.handoff_reason == "ai_unavailable"
    assert run_sql(
        "SELECT status::text FROM ai_runs WHERE id = :r", r=str(result.run_id)
    ) == [("failed",)]
    assert run_sql(
        "SELECT ai_active FROM conversations WHERE id = :c", c=c["conv"]
    ) == [(False,)]
