"""WebWidgetAdapter (docs/integrations/WEBSITE_CHAT.md §7).

The widget has no external provider: visitors talk to our REST API
(app/api/v1/endpoints/widget.py) and listen on /ws/widget. Sending a message
means publishing it to the visitor's realtime channel; a visitor who was
offline fetches it over REST when the widget reconnects.
"""

import secrets
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import BaseChannelAdapter, RetryableChannelError
from app.channels.types import OutboundAttachment, OutboundMessage, SendResult
from app.core.crypto import KEY_VERSION, encrypt_secret
from app.core.realtime import contact_channel, publish
from app.core.security import utcnow
from app.models import ChannelConnection, ChannelRoute, Inbox, WebWidget
from app.models.enums import ChannelProvider, ConnectionStatus


def new_identity_secret() -> str:
    return secrets.token_hex(32)


class WebWidgetAdapter(BaseChannelAdapter):
    provider = ChannelProvider.WEB_WIDGET

    def __init__(self, redis: Redis) -> None:  # no provider credentials
        self.redis = redis

    async def send_message(self, message: OutboundMessage) -> SendResult:
        data = {"message": dict(message.payload)}
        for token in message.recipients:
            ok = await publish(
                self.redis,
                contact_channel(token),
                message.event,
                data,
                audience="contact",
            )
            if not ok:
                raise RetryableChannelError("realtime publish failed")
        return SendResult(external_id=str(message.message_id))

    async def send_attachment(
        self, message: OutboundMessage, attachment: OutboundAttachment
    ) -> SendResult:
        # The payload already lists the attachments (signed download URLs).
        return await self.send_message(message)

    # --- connect / disconnect --------------------------------------------------

    @staticmethod
    async def create_connection(
        session: AsyncSession,
        inbox: Inbox,
        *,
        connected_by: uuid.UUID | None,
        widget_fields: dict[str, Any],
    ) -> tuple[WebWidget, ChannelConnection, str]:
        """Create the connection, the widget (website token + identity secret)
        and the route. Returns the identity secret: shown once, stored encrypted."""
        now = utcnow()
        connection = ChannelConnection(
            id=uuid.uuid4(),
            organization_id=inbox.organization_id,
            workspace_id=inbox.workspace_id,
            inbox_id=inbox.id,
            provider=ChannelProvider.WEB_WIDGET,
            status=ConnectionStatus.ACTIVE,
            connected_by=connected_by,
            connected_at=now,
        )
        session.add(connection)
        identity_secret = new_identity_secret()
        widget = WebWidget(
            id=uuid.uuid4(),
            organization_id=inbox.organization_id,
            workspace_id=inbox.workspace_id,
            channel_connection_id=connection.id,
            website_token=secrets.token_urlsafe(18),
            identity_hmac_secret_ciphertext=encrypt_secret(identity_secret),
            identity_hmac_key_version=KEY_VERSION,
            **widget_fields,
        )
        session.add(widget)
        await session.flush()
        session.add(
            ChannelRoute(
                provider=ChannelProvider.WEB_WIDGET,
                external_id=widget.website_token,
                organization_id=inbox.organization_id,
                workspace_id=inbox.workspace_id,
                inbox_id=inbox.id,
                channel_connection_id=connection.id,
            )
        )
        return widget, connection, identity_secret

    @staticmethod
    async def remove_connection(
        session: AsyncSession, widget: WebWidget, connection: ChannelConnection
    ) -> None:
        """Delete the route: /widget/config answers 404 and visitor tokens stop
        working. The inbox and its conversations stay."""
        await session.execute(
            delete(ChannelRoute).where(
                ChannelRoute.provider == ChannelProvider.WEB_WIDGET,
                ChannelRoute.external_id == widget.website_token,
            )
        )
        connection.status = ConnectionStatus.DISCONNECTED
        connection.disconnected_at = utcnow()
