"""Register, login, remember me, refresh rotation, logout, forgot/reset password."""

import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tests.conftest import API, PASSWORD, bearer, signup, unique_email


def _hours_until(iso: str) -> float:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt - datetime.now(timezone.utc)) / timedelta(hours=1)


def test_signup_creates_org_workspace_owner(client: TestClient) -> None:
    data = signup(client, org="Shree Traders")
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] == 15 * 60
    [m] = data["memberships"]
    assert m["organization_name"] == "Shree Traders"
    assert m["role"] == "owner"
    assert "billing.manage" in m["permissions"]
    assert [w["slug"] for w in m["workspaces"]] == ["main"]


def test_signup_duplicate_email_is_409(client: TestClient) -> None:
    email = unique_email()
    signup(client, email=email)
    r = client.post(
        f"{API}/auth/signup",
        json={
            "name": "X",
            "email": email.upper(),  # emails are case-insensitive
            "password": PASSWORD,
            "organization_name": "Other",
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "duplicate"


def test_signup_rejects_short_password(client: TestClient) -> None:
    r = client.post(
        f"{API}/auth/signup",
        json={
            "name": "X",
            "email": unique_email(),
            "password": "short",
            "organization_name": "O",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_login_success_and_generic_failures(client: TestClient) -> None:
    data = signup(client)
    ok = client.post(
        f"{API}/auth/login", json={"email": data["_email"], "password": PASSWORD}
    )
    assert ok.status_code == 200
    assert ok.json()["memberships"][0]["role"] == "owner"

    wrong_pw = client.post(
        f"{API}/auth/login",
        json={"email": data["_email"], "password": "nope-nope-nope"},
    )
    unknown = client.post(
        f"{API}/auth/login", json={"email": unique_email(), "password": PASSWORD}
    )
    assert wrong_pw.status_code == unknown.status_code == 401
    # Same error whether or not the account exists.
    assert wrong_pw.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert wrong_pw.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_login_is_rate_limited(client: TestClient) -> None:
    body = {"email": unique_email(), "password": "wrong-password-1"}
    codes = [client.post(f"{API}/auth/login", json=body).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429


def test_remember_me_controls_session_length_and_cookie(client: TestClient) -> None:
    email = unique_email()
    signup(client, email=email)

    short = client.post(
        f"{API}/auth/login",
        json={"email": email, "password": PASSWORD, "remember_me": False},
    )
    assert short.json()["remember_me"] is False
    assert 23 < _hours_until(short.json()["refresh_expires_at"]) <= 24
    # Browser-session cookie: no Max-Age, cleared when the browser closes.
    assert "Max-Age" not in short.headers["set-cookie"]

    long = client.post(
        f"{API}/auth/login",
        json={"email": email, "password": PASSWORD, "remember_me": True},
    )
    assert long.json()["remember_me"] is True
    assert 29 * 24 < _hours_until(long.json()["refresh_expires_at"]) <= 30 * 24
    cookie = long.headers["set-cookie"]
    assert "Max-Age=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie


def test_refresh_rotates_and_detects_reuse(client: TestClient) -> None:
    data = signup(client, remember_me=True)
    r1 = data["refresh_token"]

    rotated = client.post(f"{API}/auth/refresh", json={"refresh_token": r1})
    assert rotated.status_code == 200
    r2 = rotated.json()["refresh_token"]
    assert r2 != r1
    assert rotated.json()["remember_me"] is True  # window is preserved
    assert (
        client.get(f"{API}/auth/me", headers=bearer(rotated.json())).status_code == 200
    )

    # Re-using the old token = theft signal → whole family revoked.
    reuse = client.post(f"{API}/auth/refresh", json={"refresh_token": r1})
    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "session_revoked"
    assert (
        client.post(f"{API}/auth/refresh", json={"refresh_token": r2}).status_code
        == 401
    )
    assert (
        client.get(f"{API}/auth/me", headers=bearer(rotated.json())).status_code == 401
    )


def test_web_client_gets_cookie_only_and_needs_csrf_header(client: TestClient) -> None:
    web = {"X-Client-Type": "web"}
    data = signup(client, headers=web)
    assert data["refresh_token"] is None
    cookie = client.cookies.get("refresh_token")
    assert cookie

    # Cookie-based refresh without the CSRF header is refused.
    no_csrf = client.post(f"{API}/auth/refresh", headers=web)
    assert no_csrf.status_code == 401
    ok = client.post(
        f"{API}/auth/refresh", headers={**web, "X-Requested-With": "XMLHttpRequest"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["refresh_token"] is None
    client.cookies.clear()


def test_logout_revokes_access_and_refresh(client: TestClient) -> None:
    data = signup(client)
    assert client.post(f"{API}/auth/logout", headers=bearer(data)).status_code == 204
    me = client.get(f"{API}/auth/me", headers=bearer(data))
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "session_revoked"
    again = client.post(
        f"{API}/auth/refresh", json={"refresh_token": data["refresh_token"]}
    )
    assert again.status_code == 401


def test_logout_all_revokes_every_device(client: TestClient) -> None:
    data = signup(client)
    other = client.post(
        f"{API}/auth/login", json={"email": data["_email"], "password": PASSWORD}
    ).json()
    assert (
        client.post(f"{API}/auth/logout-all", headers=bearer(data)).status_code == 204
    )
    assert client.get(f"{API}/auth/me", headers=bearer(other)).status_code == 401


def test_me_requires_token(client: TestClient) -> None:
    r = client.get(f"{API}/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"
    bad = client.get(f"{API}/auth/me", headers={"Authorization": "Bearer junk"})
    assert bad.status_code == 401


def test_forgot_and_reset_password(
    client: TestClient, sent_emails: list[dict[str, str]]
) -> None:
    data = signup(client)
    email = data["_email"]

    # Unknown email: same 202, nothing sent.
    r = client.post(f"{API}/auth/password/forgot", json={"email": unique_email()})
    assert r.status_code == 202
    assert sent_emails == []

    r = client.post(f"{API}/auth/password/forgot", json={"email": email})
    assert r.status_code == 202
    [mail] = sent_emails
    assert mail["to"] == email
    match = re.search(r"token=([\w.\-]+)", mail["body"])
    assert match is not None
    token = match.group(1)

    new_password = "Brand-new-password-2"
    ok = client.post(
        f"{API}/auth/password/reset",
        json={"token": token, "new_password": new_password},
    )
    assert ok.status_code == 200, ok.text

    # Single use.
    reuse = client.post(
        f"{API}/auth/password/reset",
        json={"token": token, "new_password": "Another-pw-123"},
    )
    assert reuse.status_code == 401
    # Existing sessions were signed out.
    assert client.get(f"{API}/auth/me", headers=bearer(data)).status_code == 401
    # Old password no longer works; new one does.
    old = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert old.status_code == 401
    new = client.post(
        f"{API}/auth/login", json={"email": email, "password": new_password}
    )
    assert new.status_code == 200


def test_reset_with_garbage_token(client: TestClient) -> None:
    r = client.post(
        f"{API}/auth/password/reset",
        json={"token": "not-a-token", "new_password": "Whatever-123456"},
    )
    assert r.status_code == 401
