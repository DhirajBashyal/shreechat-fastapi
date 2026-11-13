"""WhatsAppCloudAdapter (WHATSAPP.md). No database access."""

from collections.abc import Mapping
from typing import Any, ClassVar

import httpx

from app.channels.base import (
    BaseChannelAdapter,
    PermanentChannelError,
    WindowClosedError,
)
from app.channels.meta.common.graph import GraphClient
from app.channels.meta.common.signature import verify_meta_signature
from app.channels.meta.common.window import whatsapp_free_form_allowed
from app.channels.meta.whatsapp import parser
from app.channels.types import (
    ConnectResult,
    HealthResult,
    InboundAttachment,
    InboundEvent,
    OutboundAttachment,
    OutboundMessage,
    RouteKey,
    SendResult,
)

MEDIA_SEND_TYPE = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "file": "document",
    "sticker": "sticker",
}
NUMBER_FIELDS = "display_phone_number,verified_name,quality_rating,code_verification_status,messaging_limit_tier"
HEALTH_FIELDS = "display_phone_number,verified_name,quality_rating,messaging_limit_tier,status,name_status"


class WhatsAppCloudAdapter(BaseChannelAdapter):
    provider = "whatsapp_cloud"
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {"attachments", "templates", "read_receipts", "reactions"}
    )

    @property
    def graph(self) -> GraphClient:
        return GraphClient(
            self.http, self.secrets.get("access_token"), product=self.provider
        )

    # --- inbound ---------------------------------------------------------------
    @classmethod
    def verify_webhook(
        cls, headers: Mapping[str, str], raw_body: bytes, secrets: list[bytes]
    ) -> bool:
        return verify_meta_signature(headers, raw_body, secrets)

    @classmethod
    def route_keys(cls, payload: Mapping[str, Any]) -> list[RouteKey]:
        return parser.route_keys(dict(payload))

    def parse_incoming(self, unit: Mapping[str, Any]) -> list[InboundEvent]:
        return parser.parse_unit(dict(unit))

    async def download_media(
        self, attachment: InboundAttachment, max_bytes: int
    ) -> tuple[bytes, str | None]:
        if not attachment.provider_media_id:
            raise PermanentChannelError("no media id", code="media_unavailable")
        info = await self.graph.get(attachment.provider_media_id)
        url = info.get("url")
        if not url:
            raise PermanentChannelError("media has no url", code="media_unavailable")
        if info.get("file_size") and int(info["file_size"]) > max_bytes:
            raise PermanentChannelError("media too large", code="media_too_large")
        data, mime = await self.graph.get_bytes(url, max_bytes=max_bytes)
        return data, info.get("mime_type") or mime

    # --- outbound -----------------------------------------------------------------
    def _base(self, message: OutboundMessage) -> dict[str, Any]:
        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.recipient_source_id,
        }
        if message.in_reply_to_external_id:
            body["context"] = {"message_id": message.in_reply_to_external_id}
        return body

    async def _post(self, body: dict[str, Any]) -> str:
        result = await self.graph.post(
            f"{self.connection.external_id}/messages", json=body
        )
        messages = result.get("messages") or [{}]
        external_id = messages[0].get("id")
        if not external_id:
            raise PermanentChannelError(
                "no message id in response", code="provider_error"
            )
        return str(external_id)

    async def send_message(self, message: OutboundMessage) -> SendResult:
        is_template = message.content_type == "template"
        if not is_template and not whatsapp_free_form_allowed(message.last_incoming_at):
            raise WindowClosedError(
                "More than 24 hours since the customer's last message: use a template"
            )
        if is_template:
            tpl = message.content_attributes.get("template") or {}
            body = self._base(message) | {
                "type": "template",
                "template": {
                    "name": tpl.get("name"),
                    "language": {"code": tpl.get("language")},
                    "components": tpl.get("graph_components") or [],
                },
            }
            return SendResult(external_id=await self._post(body))
        interactive = message.content_attributes.get("interactive")
        if interactive:
            body = self._base(message) | {
                "type": "interactive",
                "interactive": interactive,
            }
            return SendResult(external_id=await self._post(body))
        first: str | None = None
        attachments = list(message.attachments)
        # One media per message; the text becomes the first media's caption.
        caption = message.text
        for att in attachments:
            result = await self.send_attachment(
                message.model_copy(update={"text": caption}), att
            )
            caption = None
            first = first or result.external_id
        if caption:
            body = self._base(message) | {
                "type": "text",
                "text": {"body": caption, "preview_url": False},
            }
            external = await self._post(body)
            first = first or external
        if first is None:
            raise PermanentChannelError("empty message", code="empty_message")
        return SendResult(external_id=first)

    async def send_attachment(
        self, message: OutboundMessage, attachment: OutboundAttachment
    ) -> SendResult:
        kind = MEDIA_SEND_TYPE.get(attachment.kind)
        if kind is None:
            raise PermanentChannelError(
                f"can't send {attachment.kind}", code="unsupported_attachment"
            )
        media: dict[str, Any] = {"link": attachment.url}
        if message.text and kind in ("image", "video", "document"):
            media["caption"] = message.text
        if kind == "document" and attachment.filename:
            media["filename"] = attachment.filename
        body = self._base(message) | {"type": kind, kind: media}
        return SendResult(external_id=await self._post(body))

    async def mark_read(
        self, recipient_source_id: str, external_id: str | None
    ) -> None:
        if external_id:
            await self.graph.post(
                f"{self.connection.external_id}/messages",
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": external_id,
                },
            )

    async def health_check(self) -> HealthResult:
        try:
            info = await self.graph.get(
                self.connection.external_id, params={"fields": HEALTH_FIELDS}
            )
        except PermanentChannelError as exc:
            return HealthResult(ok=False, error=exc.code)
        bad = str(info.get("quality_rating", "")).upper() == "RED" or str(
            info.get("status", "")
        ).upper() in (
            "FLAGGED",
            "RESTRICTED",
        )
        return HealthResult(ok=not bad, details=info)

    # --- setup (called by the connect service; still no DB) -------------------------
    @staticmethod
    async def validate_manual(
        http: httpx.AsyncClient,
        *,
        waba_id: str,
        phone_number_id: str,
        access_token: str,
    ) -> ConnectResult:
        """WHATSAPP.md §3.1: the token reads the number, the number belongs to
        the WABA, the app is subscribed to the WABA, templates are read."""
        graph = GraphClient(http, access_token, product="whatsapp_cloud")
        number = await graph.get(phone_number_id, params={"fields": NUMBER_FIELDS})
        numbers = await graph.paginate(f"{waba_id}/phone_numbers")
        if not any(str(n.get("id")) == phone_number_id for n in numbers):
            raise PermanentChannelError(
                "The number doesn't belong to this WABA", code="number_not_in_waba"
            )
        await graph.post(f"{waba_id}/subscribed_apps")
        templates = await WhatsAppCloudAdapter.fetch_templates(
            http, waba_id=waba_id, access_token=access_token
        )
        return ConnectResult(
            external_id=phone_number_id,
            display_name=number.get("verified_name")
            or number.get("display_phone_number"),
            details={"number": number, "templates": templates},
            secrets={"access_token": access_token},
        )

    @staticmethod
    async def fetch_templates(
        http: httpx.AsyncClient, *, waba_id: str, access_token: str
    ) -> list[dict[str, Any]]:
        graph = GraphClient(http, access_token, product="whatsapp_cloud")
        return await graph.paginate(
            f"{waba_id}/message_templates",
            params={
                "limit": 100,
                "fields": "id,name,language,status,category,components,parameter_format",
            },
        )

    @staticmethod
    async def exchange_embedded_signup_code(
        http: httpx.AsyncClient,
        *,
        code: str,
        app_id: str,
        app_secret: str,
        waba_id: str,
    ) -> str:
        """Embedded Signup (R1): code → business integration system user token,
        checked with debug_token to include the WABA."""
        graph = GraphClient(http, None, product="whatsapp_cloud")
        result = await graph.get(
            "oauth/access_token",
            params={"client_id": app_id, "client_secret": app_secret, "code": code},
            auth=False,
        )
        token = result.get("access_token")
        if not token:
            raise PermanentChannelError(
                "No token from the code", code="invalid_credentials"
            )
        debug = await graph.get(
            "debug_token", params={"input_token": token}, token=f"{app_id}|{app_secret}"
        )
        scopes = (debug.get("data") or {}).get("granular_scopes") or []
        targets = {str(t) for s in scopes for t in (s.get("target_ids") or [])}
        if targets and waba_id not in targets:
            raise PermanentChannelError(
                "The token doesn't grant this WABA", code="invalid_credentials"
            )
        return str(token)
