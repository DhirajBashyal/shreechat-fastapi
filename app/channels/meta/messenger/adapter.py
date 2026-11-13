"""MessengerAdapter (FACEBOOK_MESSENGER.md). No database access."""

from typing import Any, ClassVar

import httpx

from app.channels.base import ChannelError, PermanentChannelError
from app.channels.meta.common.graph import GraphClient
from app.channels.meta.common.messaging_adapter import MessagingAdapter
from app.channels.types import HealthResult, SenderProfile

SUBSCRIBED_FIELDS = (
    "messages",
    "messaging_postbacks",
    "message_echoes",
    "message_deliveries",
    "message_reads",
    "messaging_referrals",
    "message_reactions",
)


class MessengerAdapter(MessagingAdapter):
    provider = "facebook_messenger"
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {"attachments", "read_receipts", "typing", "reactions"}
    )
    webhook_object = "page"

    @property
    def graph(self) -> GraphClient:
        return GraphClient(
            self.http, self.secrets.get("page_access_token"), product=self.provider
        )

    @property
    def send_path(self) -> str:
        return "me/messages"

    async def fetch_profile(self, source_id: str) -> SenderProfile | None:
        try:
            p = await self.graph.get(
                source_id, params={"fields": "first_name,last_name,profile_pic"}
            )
        except ChannelError:
            return None  # privacy settings (2018218) etc.: non-fatal
        name = (
            " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or None
        )
        return SenderProfile(
            source_id=source_id, name=name, avatar_url=p.get("profile_pic")
        )

    async def health_check(self) -> HealthResult:
        try:
            me = await self.graph.get("me", params={"fields": "id,name"})
        except PermanentChannelError as exc:
            return HealthResult(ok=False, error=exc.code)
        return HealthResult(ok=True, details=me)

    async def disconnect(self) -> None:
        await self._unsubscribe(f"{self.connection.external_id}/subscribed_apps")

    # --- OAuth / connect (no DB) ----------------------------------------------------
    @staticmethod
    async def exchange_code(
        http: httpx.AsyncClient,
        *,
        code: str,
        redirect_uri: str,
        app_id: str,
        app_secret: str,
    ) -> str:
        """code → short-lived user token → long-lived user token."""
        graph = GraphClient(http, None, product="facebook_messenger")
        short = await graph.get(
            "oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            auth=False,
        )
        long = await graph.get(
            "oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short.get("access_token"),
            },
            auth=False,
        )
        token = long.get("access_token") or short.get("access_token")
        if not token:
            raise PermanentChannelError("No user token", code="invalid_credentials")
        return str(token)

    @staticmethod
    async def list_pages(
        http: httpx.AsyncClient, user_token: str
    ) -> list[dict[str, Any]]:
        graph = GraphClient(http, user_token, product="facebook_messenger")
        pages = await graph.paginate(
            "me/accounts",
            params={"fields": "id,name,access_token,tasks,instagram_business_account"},
        )
        return [
            {
                "page_id": str(p.get("id")),
                "page_name": p.get("name"),
                "access_token": p.get("access_token"),
                "tasks": p.get("tasks") or [],
                "instagram_id": (p.get("instagram_business_account") or {}).get("id"),
            }
            for p in pages
            if p.get("id") and p.get("access_token")
        ]

    @staticmethod
    async def subscribe_page(
        http: httpx.AsyncClient, *, page_id: str, page_token: str
    ) -> list[str]:
        graph = GraphClient(http, page_token, product="facebook_messenger")
        await graph.post(
            f"{page_id}/subscribed_apps",
            params={"subscribed_fields": ",".join(SUBSCRIBED_FIELDS)},
        )
        return list(SUBSCRIBED_FIELDS)
