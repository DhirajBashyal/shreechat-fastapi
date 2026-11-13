"""InstagramAdapter (INSTAGRAM.md): Instagram API with Instagram Login. No DB access."""

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx

from app.channels.base import ChannelError, PermanentChannelError
from app.channels.meta.common.graph import GraphClient
from app.channels.meta.common.messaging_adapter import MessagingAdapter
from app.channels.types import ConnectResult, HealthResult, SenderProfile
from app.core.config import settings

SUBSCRIBED_FIELDS = (
    "messages",
    "messaging_postbacks",
    "messaging_seen",
    "message_reactions",
)


def ig_graph(
    http: httpx.AsyncClient, token: str | None, *, versioned: bool = True
) -> GraphClient:
    return GraphClient(
        http,
        token,
        product="instagram",
        base_url=settings.META_INSTAGRAM_GRAPH_URL,
        version=settings.META_GRAPH_API_VERSION if versioned else "",
    )


class InstagramAdapter(MessagingAdapter):
    provider = "instagram"
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {"attachments", "read_receipts", "reactions"}
    )
    webhook_object = "instagram"
    instagram = True
    sends_metadata = False  # Instagram echoes don't carry metadata

    @property
    def graph(self) -> GraphClient:
        return ig_graph(self.http, self.secrets.get("access_token"))

    @property
    def send_path(self) -> str:
        return f"{self.connection.external_id}/messages"

    async def fetch_profile(self, source_id: str) -> SenderProfile | None:
        try:
            p = await self.graph.get(
                source_id, params={"fields": "name,username,profile_pic"}
            )
        except ChannelError:
            return None
        return SenderProfile(
            source_id=source_id,
            name=p.get("name") or p.get("username"),
            username=p.get("username"),
            avatar_url=p.get("profile_pic"),
        )

    async def health_check(self) -> HealthResult:
        try:
            me = await self.graph.get("me", params={"fields": "user_id,username"})
        except PermanentChannelError as exc:
            return HealthResult(ok=False, error=exc.code)
        return HealthResult(ok=True, details=me)

    async def disconnect(self) -> None:
        await self._unsubscribe(f"{self.connection.external_id}/subscribed_apps")

    async def refresh_credentials(self) -> dict[str, Any] | None:
        """Long-lived tokens (~60 days) can be refreshed once 24 h old."""
        graph = ig_graph(self.http, None, versioned=False)
        result = await graph.get(
            "refresh_access_token",
            params={
                "grant_type": "ig_refresh_token",
                "access_token": self.secrets.get("access_token"),
            },
            auth=False,
        )
        token = result.get("access_token")
        if not token:
            raise PermanentChannelError(
                "refresh returned no token", code="reauthorization_required"
            )
        expires_in = int(result.get("expires_in") or 60 * 86400)
        return {
            "secrets": {"access_token": str(token)},
            "token_expires_at": datetime.now(UTC) + timedelta(seconds=expires_in),
        }

    # --- OAuth / connect (no DB) ----------------------------------------------------
    @staticmethod
    async def exchange_code(
        http: httpx.AsyncClient,
        *,
        code: str,
        redirect_uri: str,
        app_id: str,
        app_secret: str,
    ) -> ConnectResult:
        """code → short-lived token → long-lived token → /me."""
        oauth = GraphClient(
            http,
            None,
            product="instagram",
            base_url=settings.META_INSTAGRAM_OAUTH_URL,
            version="",
        )
        short = await oauth.post(
            "oauth/access_token",
            data={
                "client_id": app_id,
                "client_secret": app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            },
            auth=False,
        )
        if isinstance(short.get("data"), list) and short["data"]:
            short = short["data"][0]
        short_token = short.get("access_token")
        if not short_token:
            raise PermanentChannelError(
                "No Instagram token", code="invalid_credentials"
            )
        graph = ig_graph(http, None, versioned=False)
        long = await graph.get(
            "access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": app_secret,
                "access_token": short_token,
            },
            auth=False,
        )
        token = str(long.get("access_token") or short_token)
        expires_in = int(long.get("expires_in") or 3600)
        me = await ig_graph(http, token).get(
            "me", params={"fields": "user_id,username,name,profile_picture_url"}
        )
        ig_user_id = str(me.get("user_id") or short.get("user_id") or me.get("id"))
        return ConnectResult(
            external_id=ig_user_id,
            display_name=me.get("username") or me.get("name"),
            details={"username": me.get("username"), "name": me.get("name")},
            secrets={"access_token": token},
            token_expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        )

    @staticmethod
    async def subscribe(
        http: httpx.AsyncClient, *, ig_user_id: str, token: str
    ) -> list[str]:
        await ig_graph(http, token).post(
            f"{ig_user_id}/subscribed_apps",
            params={"subscribed_fields": ",".join(SUBSCRIBED_FIELDS)},
        )
        return list(SUBSCRIBED_FIELDS)
