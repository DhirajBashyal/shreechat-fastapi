"""Normalised channel types (docs/integrations/CHANNEL_ADAPTER_ARCHITECTURE.md §2).

Adapters translate provider payloads to and from these. The conversation core
never sees provider payloads.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

AttachmentKindName = Literal[
    "image", "audio", "video", "file", "location", "sticker", "fallback"
]


class InboundAttachment(BaseModel):
    kind: AttachmentKindName
    provider_media_id: str | None = None  # WhatsApp media id
    url: str | None = None  # Messenger/Instagram CDN url (expires)
    mime_type: str | None = None
    filename: str | None = None
    sha256: str | None = None
    is_voice: bool = False
    latitude: float | None = None
    longitude: float | None = None
    title: str | None = None


class SenderProfile(BaseModel):
    source_id: str  # PSID / IGSID / wa_id or BSUID
    name: str | None = None
    phone_number: str | None = None  # E.164, WhatsApp only
    username: str | None = None  # Instagram
    avatar_url: str | None = None
    alternate_source_ids: list[str] = Field(default_factory=list)


class InboundMessage(BaseModel):
    kind: Literal["message"] = "message"
    external_id: str
    sender: SenderProfile
    direction: Literal["incoming", "outgoing_echo"] = "incoming"
    sent_at: datetime
    text: str | None = None
    # text | interactive | location | contact_card | unsupported
    content_type: str = "text"
    attachments: list[InboundAttachment] = Field(default_factory=list)
    in_reply_to_external_id: str | None = None
    # Our messages.id when the provider echoes it back (Messenger metadata).
    echo_metadata: str | None = None
    content_attributes: dict[str, Any] = Field(default_factory=dict)
    # Conversation-level data, e.g. {"meta_referral": {...}}
    conversation_attributes: dict[str, Any] = Field(default_factory=dict)


class DeliveryUpdate(BaseModel):
    kind: Literal["delivery"] = "delivery"
    status: Literal["sent", "delivered", "read", "failed"]
    external_id: str | None = None  # WhatsApp / Instagram read: one message
    watermark: datetime | None = None  # Messenger: all messages up to this time
    recipient_source_id: str
    error_code: str | None = None
    error_message: str | None = None
    occurred_at: datetime
    billing: dict[str, Any] | None = None


class ReactionEvent(BaseModel):
    """Not a timeline message: applied to the target message."""

    kind: Literal["reaction"] = "reaction"
    sender: SenderProfile
    target_external_id: str
    emoji: str | None  # None / "" = removed
    occurred_at: datetime


class DeletionEvent(BaseModel):
    kind: Literal["deletion"] = "deletion"
    sender: SenderProfile
    external_id: str
    occurred_at: datetime


class ReferralEvent(BaseModel):
    kind: Literal["referral"] = "referral"
    sender: SenderProfile
    referral: dict[str, Any]
    occurred_at: datetime


InboundEvent = (
    InboundMessage | DeliveryUpdate | ReactionEvent | DeletionEvent | ReferralEvent
)


class OutboundAttachment(BaseModel):
    kind: AttachmentKindName
    url: str  # fetchable by the provider (public HTTPS / signed)
    filename: str | None = None
    mime_type: str | None = None


class OutboundMessage(BaseModel):
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    recipient_source_id: str = ""
    # Website widget: realtime tokens of the contact's identities.
    recipients: tuple[str, ...] = ()
    text: str | None = None
    content_type: str = "text"
    content_attributes: dict[str, Any] = Field(default_factory=dict)
    attachments: list[OutboundAttachment] = Field(default_factory=list)
    in_reply_to_external_id: str | None = None
    last_incoming_at: datetime | None = None
    # user = a human agent (may use HUMAN_AGENT tag); ai_agent / automation / system
    sender_kind: str = "user"
    # Website widget: the message in the visitor's shape, and the event name.
    payload: dict[str, Any] = Field(default_factory=dict)
    event: str = "message.created"


class SendResult(BaseModel):
    external_id: str | None
    status: Literal["sent", "pending"] = "sent"


@dataclass(frozen=True)
class RouteKey:
    """One unit of a webhook delivery (§3 route_keys)."""

    routing_id: str  # page id / ig user id / phone_number_id
    dedup_key: str
    ordering_key: str  # customer id: one lock per (inbox, customer)
    event_type: str  # message | status | echo | read | delivery | template_status | ...
    payload: dict[str, Any]


@dataclass(frozen=True)
class ConnectionView:
    """What an adapter knows about its connection: ids + config, no ORM row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    workspace_id: uuid.UUID
    inbox_id: uuid.UUID
    provider: str
    status: str
    external_id: str  # the routing id
    config: dict[str, Any] = field(default_factory=dict)
    token_expires_at: datetime | None = None


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ConnectResult:
    """What connect() learned; the service persists it."""

    external_id: str
    display_name: str | None
    details: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    token_expires_at: datetime | None = None
