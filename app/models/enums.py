"""PostgreSQL enum types (docs/database/INITIAL_SCHEMA_SQL.md, "Enumerated types")."""

import enum
from typing import Any

from sqlalchemy import Enum as SAEnum


class OrgStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    PENDING_DELETION = "pending_deletion"


class MembershipRole(enum.StrEnum):
    OWNER = "owner"
    ADMINISTRATOR = "administrator"
    AGENT = "agent"


class AvailabilityStatus(enum.StrEnum):
    ONLINE = "online"
    BUSY = "busy"
    OFFLINE = "offline"


class ChannelProvider(enum.StrEnum):
    WEB_WIDGET = "web_widget"
    FACEBOOK_MESSENGER = "facebook_messenger"
    INSTAGRAM = "instagram"
    WHATSAPP_CLOUD = "whatsapp_cloud"
    EMAIL = "email"
    API = "api"


class ConnectionStatus(enum.StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class ConversationStatus(enum.StrEnum):
    OPEN = "open"
    PENDING = "pending"
    SNOOZED = "snoozed"
    RESOLVED = "resolved"


class ConversationPriority(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class MessageDirection(enum.StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    ACTIVITY = "activity"
    TEMPLATE = "template"


class MessageContentType(enum.StrEnum):
    TEXT = "text"
    INPUT_SELECT = "input_select"
    CARDS = "cards"
    FORM = "form"
    ARTICLE = "article"
    INTERACTIVE = "interactive"
    TEMPLATE = "template"
    LOCATION = "location"
    CONTACT_CARD = "contact_card"
    UNSUPPORTED = "unsupported"


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class SenderKind(enum.StrEnum):
    USER = "user"
    CONTACT = "contact"
    AI_AGENT = "ai_agent"
    SYSTEM = "system"
    AUTOMATION = "automation"


class AttachmentKind(enum.StrEnum):
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    LOCATION = "location"
    STICKER = "sticker"
    FALLBACK = "fallback"


class AttributeScope(enum.StrEnum):
    CONVERSATION = "conversation"
    CONTACT = "contact"


class AttributeValueType(enum.StrEnum):
    TEXT = "text"
    NUMBER = "number"
    CURRENCY = "currency"
    PERCENT = "percent"
    LINK = "link"
    DATE = "date"
    LIST = "list"
    CHECKBOX = "checkbox"


class WebhookDeliveryState(enum.StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


class InboundEventState(enum.StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"
    DEAD = "dead"


class OutboxState(enum.StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    FAILED = "failed"


class AiRunStatus(enum.StrEnum):
    RUNNING = "running"
    REPLIED = "replied"
    HANDED_OFF = "handed_off"
    DISCARDED = "discarded"
    FAILED = "failed"


class KbDocumentStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class SubscriptionState(enum.StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"


class BillingRecordState(enum.StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    VOID = "void"
    UNCOLLECTIBLE = "uncollectible"


def pg_enum(enum_cls: type[enum.StrEnum], name: str) -> Any:
    """Map a StrEnum to a named PostgreSQL enum, storing the lowercase values."""
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


ORG_STATUS = pg_enum(OrgStatus, "org_status")
MEMBERSHIP_ROLE = pg_enum(MembershipRole, "membership_role")
AVAILABILITY_STATUS = pg_enum(AvailabilityStatus, "availability_status")
CHANNEL_PROVIDER = pg_enum(ChannelProvider, "channel_provider")
CONNECTION_STATUS = pg_enum(ConnectionStatus, "connection_status")
CONVERSATION_STATUS = pg_enum(ConversationStatus, "conversation_status")
CONVERSATION_PRIORITY = pg_enum(ConversationPriority, "conversation_priority")
MESSAGE_DIRECTION = pg_enum(MessageDirection, "message_direction")
MESSAGE_CONTENT_TYPE = pg_enum(MessageContentType, "message_content_type")
DELIVERY_STATUS = pg_enum(DeliveryStatus, "delivery_status")
SENDER_KIND = pg_enum(SenderKind, "sender_kind")
ATTACHMENT_KIND = pg_enum(AttachmentKind, "attachment_kind")
ATTRIBUTE_SCOPE = pg_enum(AttributeScope, "attribute_scope")
ATTRIBUTE_VALUE_TYPE = pg_enum(AttributeValueType, "attribute_value_type")
WEBHOOK_DELIVERY_STATE = pg_enum(WebhookDeliveryState, "webhook_delivery_state")
INBOUND_EVENT_STATE = pg_enum(InboundEventState, "inbound_event_state")
OUTBOX_STATE = pg_enum(OutboxState, "outbox_state")
AI_RUN_STATUS = pg_enum(AiRunStatus, "ai_run_status")
KB_DOCUMENT_STATUS = pg_enum(KbDocumentStatus, "kb_document_status")
SUBSCRIPTION_STATE = pg_enum(SubscriptionState, "subscription_state")
BILLING_RECORD_STATE = pg_enum(BillingRecordState, "billing_record_state")
