"""Website live chat (docs/integrations/WEBSITE_CHAT.md)."""

import asyncio
import hashlib
import hmac
import json
import re
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.core.realtime import contact_channel, publish
from app.services.outbox_relay import dispatch_pending
from tests.conftest import API, bearer, run_sql, signup, unique_email

W = f"{API}/widget"
ORIGIN = "https://shop.example.com"


def _ws(t: dict[str, Any]) -> str:
    return str(t["memberships"][0]["workspaces"][0]["id"])


def _org(t: dict[str, Any]) -> str:
    return str(t["memberships"][0]["organization_id"])


def _widget(client: TestClient, owner: dict[str, Any], **body: Any) -> dict[str, Any]:
    r = client.post(
        f"{API}/workspaces/{_ws(owner)}/channels/web-widget",
        json={
            "name": "Website",
            "allowed_origins": [ORIGIN],
            "greeting_enabled": True,
            "greeting_message": "Hi! How can we help?",
            **body,
        },
        headers=bearer(owner),
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _session(
    client: TestClient, token: str, visitor: str | None = None
) -> dict[str, Any]:
    r = client.post(
        f"{W}/sessions",
        json={"website_token": token, "visitor_token": visitor},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()  # type: ignore[no-any-return]


def _h(session: dict[str, Any]) -> dict[str, str]:
    return {"X-Visitor-Token": session["visitor_token"], "Origin": ORIGIN}


def _start(
    client: TestClient, token: str, s: dict[str, Any], text: str = "Hello"
) -> dict[str, Any]:
    r = client.post(
        f"{W}/conversations?website_token={token}",
        json={"content": text, "client_message_id": str(uuid.uuid4())},
        headers={
            **_h(s),
            "User-Agent": "Mozilla/5.0 (Macintosh; Mac OS X 14_0) Chrome/120.0.1 Safari/537.36",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _hash(secret: str, identifier: str) -> str:
    return hmac.new(secret.encode(), identifier.encode(), hashlib.sha256).hexdigest()


# --- Admin ----------------------------------------------------------------------


def test_create_widget_inbox_and_settings(client: TestClient) -> None:
    owner = signup(client)
    w = _widget(client, owner)
    assert w["status"] == "active"
    assert len(w["identity_secret"]) == 64 and w["identity_secret_set"]
    assert w["website_token"] in w["embed_script"]
    url = f"{API}/workspaces/{_ws(owner)}/inboxes/{w['inbox_id']}/web-widget"

    r = client.get(url, headers=bearer(owner))
    assert r.status_code == 200 and "identity_secret" not in r.json()

    r = client.patch(
        url,
        json={
            "widget_color": "#ff0000",
            "welcome_title": None,
            "timezone": "Asia/Kathmandu",
        },
        headers=bearer(owner),
    )
    assert r.status_code == 200, r.text
    assert (
        r.json()["widget_color"] == "#ff0000"
        and r.json()["timezone"] == "Asia/Kathmandu"
    )

    bad = client.patch(
        url, json={"allowed_origins": ["not a url"]}, headers=bearer(owner)
    )
    assert bad.status_code == 422

    rotated = client.post(url + "/identity-secret/rotate", headers=bearer(owner))
    assert rotated.status_code == 200
    assert rotated.json()["identity_secret"] != w["identity_secret"]


def test_agents_cannot_manage_widgets(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    owner = signup(client)
    email = unique_email("agent")
    client.post(
        f"{API}/organizations/{_org(owner)}/invitations",
        json={"email": email, "role": "agent"},
        headers=bearer(owner),
    )
    token = re.search(r"token=([\w\-]+)", sent_emails[-1]["body"])
    assert token
    agent = signup(client, email=email, invitation_token=token.group(1))
    client.patch(
        f"{API}/organizations/{_org(owner)}/members/{agent['memberships'][0]['membership_id']}",
        json={"workspace_ids": [_ws(owner)]},
        headers=bearer(owner),
    )
    r = client.post(
        f"{API}/workspaces/{_ws(owner)}/channels/web-widget",
        json={"name": "Nope"},
        headers=bearer(agent),
    )
    assert r.status_code == 403


# --- Config, sessions, origins -----------------------------------------------------


def test_config_session_and_origin_checks(client: TestClient) -> None:
    owner = signup(client)
    w = _widget(client, owner, welcome_title="Welcome!")
    token = w["website_token"]

    r = client.get(
        f"{W}/config", params={"website_token": token}, headers={"Origin": ORIGIN}
    )
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["welcome_title"] == "Welcome!" and cfg["ai_assistant"] is False
    assert cfg["is_online"] is True

    assert (
        client.get(f"{W}/config", params={"website_token": "nope"}).status_code == 404
    )
    evil = client.get(
        f"{W}/config",
        params={"website_token": token},
        headers={"Origin": "https://evil.example"},
    )
    assert (
        evil.status_code == 403 and evil.json()["error"]["code"] == "origin_not_allowed"
    )
    # Our own widget app (FRONTEND_URL) is always allowed.
    ours = client.get(
        f"{W}/config",
        params={"website_token": token},
        headers={"Origin": settings.FRONTEND_URL},
    )
    assert ours.status_code == 200
    # …but then the page embedding the iframe must be allowed.
    framed = client.get(
        f"{W}/config",
        params={"website_token": token},
        headers={
            "Origin": settings.FRONTEND_URL,
            "X-Parent-Origin": "https://evil.example",
        },
    )
    assert framed.status_code == 403
    framed_ok = client.get(
        f"{W}/config",
        params={"website_token": token},
        headers={"Origin": settings.FRONTEND_URL, "X-Parent-Origin": ORIGIN},
    )
    assert framed_ok.status_code == 200
    assert client.get(f"{W}/config").json()["error"]["code"] == "website_token_required"

    s = _session(client, token)
    assert s["resumed"] is False and s["contact"]["identity_verified"] is False
    again = _session(client, token, s["visitor_token"])
    assert again["resumed"] is True and again["realtime_token"] == s["realtime_token"]
    fresh = _session(client, token, "garbage")
    assert fresh["resumed"] is False

    # Visitor endpoints need the token.
    r = client.get(
        f"{W}/conversations",
        params={"website_token": token},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 401

    # Reset revokes the token.
    assert (
        client.request(
            "DELETE", f"{W}/sessions?website_token={token}", headers=_h(s)
        ).status_code
        == 204
    )
    r = client.get(f"{W}/conversations", params={"website_token": token}, headers=_h(s))
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_visitor_token"


def test_disconnected_widget_is_gone(client: TestClient) -> None:
    owner = signup(client)
    w = _widget(client, owner)
    s = _session(client, w["website_token"])
    url = f"{API}/workspaces/{_ws(owner)}/inboxes/{w['inbox_id']}/web-widget"
    assert client.delete(url, headers=bearer(owner)).status_code == 204
    assert client.get(url, headers=bearer(owner)).json()["status"] == "disconnected"
    r = client.get(
        f"{W}/conversations",
        params={"website_token": w["website_token"]},
        headers=_h(s),
    )
    assert r.status_code == 404


def test_body_size_limit(client: TestClient) -> None:
    r = client.post(
        f"{W}/sessions?website_token=x",
        content=b"{" + b" " * (70 * 1024) + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413


# --- Conversations and messages ----------------------------------------------------


def test_conversation_flow(client: TestClient) -> None:
    owner = signup(client)
    w = _widget(client, owner)
    token = w["website_token"]
    s = _session(client, token)

    created = _start(client, token, s, "Do you have this in blue?")
    conv = created["conversation"]
    assert conv["status"] == "open"
    kinds = [(m["direction"], m["sender"]["kind"]) for m in created["messages"]]
    assert kinds == [("incoming", "contact"), ("outgoing", "system")]
    assert created["messages"][1]["content"] == "Hi! How can we help?"

    attrs = run_sql(
        "SELECT additional_attributes FROM conversations WHERE id = :id", id=conv["id"]
    )[0][0]
    assert (
        attrs["browser"]["name"] == "Chrome" and attrs["browser"]["platform"] == "macOS"
    )

    # Idempotent send.
    url = f"{W}/conversations/{conv['id']}/messages?website_token={token}"
    body = {"content": "Size M please", "client_message_id": "c-1"}
    first = client.post(url, json=body, headers=_h(s))
    retry = client.post(url, json=body, headers=_h(s))
    assert first.status_code == 201 and retry.status_code == 200
    assert first.json()["id"] == retry.json()["id"]

    # Private notes never reach the visitor.
    ids = run_sql(
        "SELECT organization_id, workspace_id, inbox_id FROM conversations WHERE id = :id",
        id=conv["id"],
    )[0]
    run_sql(
        "INSERT INTO messages (organization_id, workspace_id, inbox_id, conversation_id, direction, content, is_private, sender_kind, sender_user_id) "
        "VALUES (:o, :w, :i, :c, 'outgoing', 'internal only', true, 'user', :u)",
        o=ids[0],
        w=ids[1],
        i=ids[2],
        c=conv["id"],
        u=owner["user"]["id"],
    )
    msgs = client.get(url, headers=_h(s)).json()
    assert [m["content"] for m in msgs] == [
        "Do you have this in blue?",
        "Hi! How can we help?",
        "Size M please",
    ]
    older = client.get(
        url + f"&before={msgs[-1]['sequence_number']}&limit=1", headers=_h(s)
    ).json()
    assert [m["content"] for m in older] == ["Hi! How can we help?"]
    newer = client.get(
        url + f"&after={msgs[0]['sequence_number']}", headers=_h(s)
    ).json()
    assert len(newer) == 2

    listed = client.get(
        f"{W}/conversations?website_token={token}", headers=_h(s)
    ).json()
    assert listed[0]["id"] == conv["id"] and listed[0]["unread_count"] == 1

    seen = client.post(
        f"{W}/conversations/{conv['id']}/seen?website_token={token}", headers=_h(s)
    )
    assert seen.status_code == 200 and seen.json()["unread_count"] == 0
    status = run_sql(
        "SELECT delivery_status FROM messages WHERE conversation_id = :c AND direction = 'outgoing' AND NOT is_private",
        c=conv["id"],
    )
    assert {r[0] for r in status} == {"read"}

    typing = client.post(
        f"{W}/conversations/{conv['id']}/typing?website_token={token}",
        json={"state": "on"},
        headers=_h(s),
    )
    assert typing.status_code == 204

    # Another visitor can't see it.
    other = _session(client, token)
    assert client.get(url, headers=_h(other)).status_code == 404

    # Ending the chat, then writing again reopens it.
    r = client.post(
        f"{W}/conversations/{conv['id']}/resolve?website_token={token}", headers=_h(s)
    )
    assert r.json()["status"] == "resolved"
    r = client.post(
        url,
        json={"content": "one more thing", "client_message_id": "c-2"},
        headers=_h(s),
    )
    assert r.status_code == 201
    assert (
        client.get(f"{W}/conversations?website_token={token}", headers=_h(s)).json()[0][
            "status"
        ]
        == "open"
    )


def test_resolve_disabled_and_resolved_locked(client: TestClient) -> None:
    owner = signup(client)
    w = _widget(
        client,
        owner,
        feature_flags=["emoji_picker"],
        allow_messages_after_resolved=False,
    )
    token = w["website_token"]
    s = _session(client, token)
    conv = _start(client, token, s)["conversation"]
    r = client.post(
        f"{W}/conversations/{conv['id']}/resolve?website_token={token}", headers=_h(s)
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "feature_disabled"
    run_sql(
        "UPDATE conversations SET status = 'resolved' WHERE id = :id", id=conv["id"]
    )
    r = client.post(
        f"{W}/conversations/{conv['id']}/messages?website_token={token}",
        json={"content": "hi", "client_message_id": "x"},
        headers=_h(s),
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "conversation_resolved"


def test_pre_chat_and_attributes_and_labels(client: TestClient) -> None:
    owner = signup(client)
    w = _widget(
        client,
        owner,
        pre_chat_form_enabled=True,
        pre_chat_form_options={"require_email": True},
    )
    token = w["website_token"]
    org, ws = _org(owner), _ws(owner)
    run_sql(
        "INSERT INTO custom_attribute_definitions (organization_id, workspace_id, scope, attribute_key, display_name, value_type, list_values) "
        "VALUES (:o, :w, 'contact', 'plan', 'Plan', 'list', ARRAY['free','pro']), "
        "(:o, :w, 'conversation', 'order_id', 'Order', 'text', '{}')",
        o=org,
        w=ws,
    )
    run_sql(
        "INSERT INTO labels (organization_id, workspace_id, title, widget_settable) VALUES (:o, :w, 'sales', true), (:o, :w, 'vip', false)",
        o=org,
        w=ws,
    )
    s = _session(client, token)
    r = client.post(
        f"{W}/conversations?website_token={token}",
        json={"content": "hi"},
        headers=_h(s),
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "pre_chat_required"
    r = client.post(
        f"{W}/conversations?website_token={token}",
        json={
            "content": "hi",
            "contact": {"name": "Ana", "email": "ana@example.com"},
            "custom_attributes": {"order_id": "A-1"},
        },
        headers=_h(s),
    )
    assert r.status_code == 201, r.text
    conv = r.json()["conversation"]
    assert conv["custom_attributes"] == {"order_id": "A-1"}

    c = f"{W}/contact?website_token={token}"
    assert (
        client.patch(
            c, json={"custom_attributes": {"plan": "gold"}}, headers=_h(s)
        ).json()["error"]["code"]
        == "invalid_attribute_value"
    )
    assert (
        client.patch(c, json={"custom_attributes": {"nope": 1}}, headers=_h(s)).json()[
            "error"
        ]["code"]
        == "unknown_attribute"
    )
    ok = client.patch(
        c,
        json={"custom_attributes": {"plan": "pro"}, "phone": "+9779800000000"},
        headers=_h(s),
    )
    assert ok.status_code == 200 and ok.json()["email"] == "ana@example.com"

    lbl = f"{W}/conversations/{conv['id']}/labels?website_token={token}"
    assert (
        client.post(lbl, json={"label": "vip"}, headers=_h(s)).json()["error"]["code"]
        == "label_not_allowed"
    )
    assert client.post(lbl, json={"label": "sales"}, headers=_h(s)).json()[
        "labels"
    ] == ["sales"]
    r = client.request(
        "DELETE",
        f"{W}/conversations/{conv['id']}/labels/sales?website_token={token}",
        headers=_h(s),
    )
    assert r.json()["labels"] == []

    assert (
        client.post(
            f"{W}/events?website_token={token}",
            json={"name": "page_view", "url": "https://shop.example.com/p/1"},
            headers=_h(s),
        ).status_code
        == 202
    )
    assert (
        client.post(
            f"{W}/uploads?website_token={token}",
            json={"filename": "a.png", "content_type": "image/png", "byte_size": 10},
            headers=_h(s),
        ).status_code
        == 503
    )


# --- Identify -------------------------------------------------------------------------


def test_unverified_identify_never_merges(client: TestClient) -> None:
    owner = signup(client)
    w = _widget(client, owner)
    token = w["website_token"]
    run_sql(
        "INSERT INTO contacts (organization_id, workspace_id, name, email, identifier) VALUES (:o, :w, 'Real Customer', 'real@example.com', 'cust-1')",
        o=_org(owner),
        w=_ws(owner),
    )
    s = _session(client, token)
    r = client.post(
        f"{W}/identify?website_token={token}",
        json={"identifier": "cust-1", "email": "real@example.com", "name": "Mallory"},
        headers=_h(s),
    )
    assert r.status_code == 200, r.text
    assert r.json()["contact"]["identity_verified"] is False
    real = run_sql(
        "SELECT name FROM contacts WHERE identifier = 'cust-1' AND workspace_id = :w",
        w=_ws(owner),
    )
    assert real[0][0] == "Real Customer"  # untouched
    # The old token was rotated.
    assert (
        client.get(
            f"{W}/conversations?website_token={token}", headers=_h(s)
        ).status_code
        == 401
    )


def test_verified_identify_merges_and_continues_on_another_device(
    client: TestClient,
) -> None:
    owner = signup(client)
    w = _widget(client, owner)
    token, secret = w["website_token"], w["identity_secret"]

    laptop = _session(client, token)
    conv = _start(client, token, laptop, "Where is my order?")["conversation"]
    bad = client.post(
        f"{W}/identify?website_token={token}",
        json={"identifier": "user-42", "identifier_hash": "0" * 64},
        headers=_h(laptop),
    )
    assert (
        bad.status_code == 403
        and bad.json()["error"]["code"] == "invalid_identifier_hash"
    )
    r = client.post(
        f"{W}/identify?website_token={token}",
        json={
            "identifier": "user-42",
            "identifier_hash": _hash(secret, "user-42"),
            "email": "u42@example.com",
        },
        headers=_h(laptop),
    )
    assert r.status_code == 200 and r.json()["contact"]["identity_verified"] is True
    laptop = r.json()

    phone = _session(client, token)
    r = client.post(
        f"{W}/identify?website_token={token}",
        json={"identifier": "user-42", "identifier_hash": _hash(secret, "user-42")},
        headers=_h(phone),
    )
    assert r.status_code == 200
    phone = r.json()
    convs = client.get(
        f"{W}/conversations?website_token={token}", headers=_h(phone)
    ).json()
    assert [c["id"] for c in convs] == [conv["id"]]
    alive = run_sql(
        "SELECT count(*) FROM contacts WHERE workspace_id = :w AND deleted_at IS NULL",
        w=_ws(owner),
    )[0][0]
    assert alive == 1  # the phone's empty visitor contact was merged away

    other = client.post(
        f"{W}/identify?website_token={token}",
        json={"identifier": "user-99", "identifier_hash": _hash(secret, "user-99")},
        headers=_h(phone),
    )
    assert (
        other.status_code == 409 and other.json()["error"]["code"] == "reset_required"
    )


def test_identity_verification_required(client: TestClient) -> None:
    owner = signup(client)
    w = _widget(client, owner, identity_verification_required=True)
    s = _session(client, w["website_token"])
    r = client.post(
        f"{W}/identify?website_token={w['website_token']}",
        json={"identifier": "someone"},
        headers=_h(s),
    )
    assert (
        r.status_code == 403
        and r.json()["error"]["code"] == "identity_verification_required"
    )


# --- AI + delivery -----------------------------------------------------------------------


def test_ai_inbox_starts_pending(client: TestClient) -> None:
    owner = signup(client)
    w = _widget(client, owner)
    r = client.post(
        f"{API}/workspaces/{_ws(owner)}/ai-agents",
        json={"name": "Ava", "chat_model": "gpt-4o-mini"},
        headers=bearer(owner),
    )
    agent = r.json()
    r = client.put(
        f"{API}/workspaces/{_ws(owner)}/ai-agents/{agent['id']}/inboxes",
        json={"inboxes": [{"inbox_id": w["inbox_id"], "mode": "autonomous"}]},
        headers=bearer(owner),
    )
    assert r.status_code == 200, r.text
    token = w["website_token"]
    assert (
        client.get(f"{W}/config?website_token={token}").json()["ai_assistant"] is True
    )
    s = _session(client, token)
    conv = _start(client, token, s)["conversation"]
    assert conv["status"] == "pending"
    row = run_sql(
        "SELECT ai_active, ai_agent_id FROM conversations WHERE id = :id", id=conv["id"]
    )[0]
    assert row[0] is True and str(row[1]) == agent["id"]


def _relay_and_listen(channel: str, before: Any = None) -> list[dict[str, Any]]:
    """Subscribe to a visitor channel, run the outbox relay, collect events."""

    async def go() -> list[dict[str, Any]]:
        engine = create_async_engine(settings.DB_URL, poolclass=NullPool)
        redis = Redis.from_url(settings.REDIS_URL)
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            if before:
                await before(redis)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            for _ in range(100):
                if not await dispatch_pending(factory, redis, batch=500):
                    break
            events: list[dict[str, Any]] = []
            # get_message returns None for the (ignored) subscribe confirmation
            # too, so collect until nothing arrived for a while.
            quiet = 0
            while quiet < 3:
                m = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.3
                )
                if m is None:
                    quiet += 1
                    continue
                quiet = 0
                events.append(json.loads(m["data"]))
            return events
        finally:
            await pubsub.aclose()
            await redis.aclose()
            await engine.dispose()

    return asyncio.run(go())


def test_relay_delivers_agent_replies_to_the_visitor(client: TestClient) -> None:
    owner = signup(client)
    w = _widget(client, owner, greeting_enabled=False)
    token = w["website_token"]
    s = _session(client, token)
    conv = _start(client, token, s)["conversation"]
    ids = run_sql(
        "SELECT organization_id, workspace_id, inbox_id FROM conversations WHERE id = :id",
        id=conv["id"],
    )[0]
    for private, text in ((False, "Yes, we have blue!"), (True, "secret note")):
        run_sql(
            "INSERT INTO messages (id, organization_id, workspace_id, inbox_id, conversation_id, direction, content, is_private, sender_kind, sender_user_id) "
            "VALUES (:m, :o, :w, :i, :c, 'outgoing', :t, :p, 'user', :u)",
            m=str(uuid.uuid4()),
            o=ids[0],
            w=ids[1],
            i=ids[2],
            c=conv["id"],
            t=text,
            p=private,
            u=owner["user"]["id"],
        )
        run_sql(
            "INSERT INTO event_outbox (organization_id, workspace_id, event_name, aggregate_type, aggregate_id, payload) "
            "SELECT organization_id, workspace_id, 'message.created', 'conversation', conversation_id, "
            "jsonb_build_object('conversation_id', conversation_id, 'message_id', id, 'private', is_private) "
            "FROM messages WHERE content = :t AND conversation_id = :c",
            t=text,
            c=conv["id"],
        )
    events = _relay_and_listen(contact_channel(s["realtime_token"]))
    contents = [
        e["data"]["message"]["content"]
        for e in events
        if e["event"] == "message.created"
    ]
    assert "Yes, we have blue!" in contents and "Hello" in contents
    assert "secret note" not in contents
    reply = next(
        e
        for e in events
        if e["data"].get("message", {}).get("content") == "Yes, we have blue!"
    )
    assert reply["data"]["message"]["sender"] == {"kind": "user", "name": "Test User"}
    pending = run_sql(
        "SELECT count(*) FROM event_outbox WHERE aggregate_id = :c AND state = 'pending'",
        c=conv["id"],
    )[0][0]
    assert pending == 0


def test_websocket_auth_and_events(client: TestClient) -> None:
    owner = signup(client)
    w = _widget(client, owner)
    token = w["website_token"]
    s = _session(client, token)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/widget?website_token=nope") as ws:
            ws.receive_json()
    assert exc.value.code == 4404

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/ws/widget?website_token={token}",
            headers={"Origin": "https://evil.example"},
        ) as ws:
            ws.receive_json()
    assert exc.value.code == 4403

    with client.websocket_connect(
        f"/ws/widget?website_token={token}", headers={"Origin": ORIGIN}
    ) as ws:
        ws.send_json({"type": "auth", "token": "bad"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401

    with client.websocket_connect(
        f"/ws/widget?website_token={token}", headers={"Origin": settings.FRONTEND_URL}
    ) as ws:
        ws.send_json(
            {
                "type": "auth",
                "token": s["visitor_token"],
                "parent_origin": "https://evil.example",
            }
        )
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4403

    with client.websocket_connect(
        f"/ws/widget?website_token={token}", headers={"Origin": ORIGIN}
    ) as ws:
        ws.send_json({"type": "auth", "token": s["visitor_token"]})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}

        async def push() -> None:
            redis = Redis.from_url(settings.REDIS_URL)
            try:
                await publish(
                    redis,
                    contact_channel(s["realtime_token"]),
                    "conversation.typing",
                    {"state": "on"},
                )
            finally:
                await redis.aclose()

        asyncio.run(push())
        event = ws.receive_json()
        assert event["type"] == "event" and event["event"] == "conversation.typing"

        ws.send_json(
            {"type": "typing", "conversation_id": str(uuid.uuid4()), "state": "on"}
        )
        assert ws.receive_json() == {"type": "error", "code": "not_found"}
