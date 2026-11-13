"""Sending through the Messenger Platform (Messenger and Instagram).

FACEBOOK_MESSENGER.md §5, INSTAGRAM.md §5: one attachment per call, text
first; ``RESPONSE`` within 24 h, ``HUMAN_AGENT`` tag for human agents up to 7
days, otherwise ``WindowClosedError``. Messenger also sets ``metadata`` to our
message id so echoes can be matched before the send result is stored.
"""

from collections.abc import Mapping
from typing import Any

from app.channels.base import (
    BaseChannelAdapter,
    PermanentChannelError,
    ReauthorizationRequiredError,
)
from app.channels.meta.common import messaging
from app.channels.meta.common.graph import GraphClient
from app.channels.meta.common.signature import verify_meta_signature
from app.channels.meta.common.window import messaging_type
from app.channels.types import (
    InboundAttachment,
    InboundEvent,
    OutboundAttachment,
    OutboundMessage,
    RouteKey,
    SendResult,
)

ATTACHMENT_TYPES = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "file": "file",
    "sticker": "image",
}


class MessagingAdapter(BaseChannelAdapter):
    webhook_object: str = "page"
    instagram: bool = False
    sends_metadata: bool = True

    @property
    def graph(self) -> GraphClient:
        raise NotImplementedError

    @property
    def send_path(self) -> str:
        raise NotImplementedError

    # --- inbound ---------------------------------------------------------------
    @classmethod
    def verify_webhook(
        cls, headers: Mapping[str, str], raw_body: bytes, secrets: list[bytes]
    ) -> bool:
        return verify_meta_signature(headers, raw_body, secrets)

    @classmethod
    def route_keys(cls, payload: Mapping[str, Any]) -> list[RouteKey]:
        return messaging.route_keys(dict(payload), obj=cls.webhook_object)

    def parse_incoming(self, unit: Mapping[str, Any]) -> list[InboundEvent]:
        return messaging.parse_item(dict(unit["item"]), instagram=self.instagram)

    async def download_media(
        self, attachment: InboundAttachment, max_bytes: int
    ) -> tuple[bytes, str | None]:
        if not attachment.url:
            raise PermanentChannelError("no media url", code="media_unavailable")
        cdn = GraphClient(
            self.http, None, product=self.provider
        )  # CDN URLs need no token
        return await cdn.get_bytes(attachment.url, max_bytes=max_bytes)

    # --- outbound -----------------------------------------------------------------
    def _envelope(self, message: OutboundMessage, mtype: str) -> dict[str, Any]:
        body: dict[str, Any] = {"recipient": {"id": message.recipient_source_id}}
        if mtype == "HUMAN_AGENT":
            body["messaging_type"] = "MESSAGE_TAG"
            body["tag"] = "HUMAN_AGENT"
        else:
            body["messaging_type"] = "RESPONSE"
        return body

    async def _post(self, body: dict[str, Any]) -> str:
        result = await self.graph.post(self.send_path, json=body)
        external = result.get("message_id")
        if not external:
            raise PermanentChannelError(
                "no message id in response", code="provider_error"
            )
        return str(external)

    async def send_message(self, message: OutboundMessage) -> SendResult:
        mtype = messaging_type(message.last_incoming_at, message.sender_kind)
        first: str | None = None
        if message.text:
            payload: dict[str, Any] = {"text": message.text}
            items = message.content_attributes.get("items")
            if message.content_type == "input_select" and isinstance(items, list):
                payload["quick_replies"] = [
                    {
                        "content_type": "text",
                        "title": str(i.get("title", ""))[:20],
                        "payload": str(i.get("value", i.get("title", ""))),
                    }
                    for i in items[:13]
                    if isinstance(i, dict)
                ]
            if self.sends_metadata:
                payload["metadata"] = str(message.message_id)
            first = await self._post(
                self._envelope(message, mtype) | {"message": payload}
            )
        for att in message.attachments:
            result = await self._send_attachment(message, att, mtype)
            first = first or result.external_id
        if first is None:
            raise PermanentChannelError("empty message", code="empty_message")
        return SendResult(external_id=first)

    async def _send_attachment(
        self, message: OutboundMessage, attachment: OutboundAttachment, mtype: str
    ) -> SendResult:
        kind = ATTACHMENT_TYPES.get(attachment.kind)
        if kind is None:
            raise PermanentChannelError(
                f"can't send {attachment.kind}", code="unsupported_attachment"
            )
        payload: dict[str, Any] = {
            "attachment": {
                "type": kind,
                "payload": {"url": attachment.url, "is_reusable": False},
            }
        }
        if self.sends_metadata:
            payload["metadata"] = str(message.message_id)
        return SendResult(
            external_id=await self._post(
                self._envelope(message, mtype) | {"message": payload}
            )
        )

    async def send_attachment(
        self, message: OutboundMessage, attachment: OutboundAttachment
    ) -> SendResult:
        return await self._send_attachment(
            message,
            attachment,
            messaging_type(message.last_incoming_at, message.sender_kind),
        )

    async def sender_action(self, recipient: str, action: str) -> None:
        """typing_on / typing_off / mark_seen: best effort, never raises."""
        try:
            await self.graph.post(
                self.send_path,
                json={"recipient": {"id": recipient}, "sender_action": action},
            )
        except Exception:
            return

    async def mark_read(
        self, recipient_source_id: str, external_id: str | None
    ) -> None:
        await self.sender_action(recipient_source_id, "mark_seen")

    async def _unsubscribe(self, path: str) -> None:
        try:
            await self.graph.delete(path)
        except ReauthorizationRequiredError:
            return  # token already invalid: nothing to unsubscribe
        except PermanentChannelError:
            return
