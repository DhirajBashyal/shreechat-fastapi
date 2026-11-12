"""Transactional outbox, inbound webhooks, outbound webhooks, idempotency keys."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    created_at_col,
    updated_at_col,
    uuid_pk,
    workspace_fk,
    ws_fk,
)
from app.models.enums import (
    CHANNEL_PROVIDER,
    INBOUND_EVENT_STATE,
    OUTBOX_STATE,
    WEBHOOK_DELIVERY_STATE,
    ChannelProvider,
    InboundEventState,
    OutboxState,
    WebhookDeliveryState,
)

DEFAULT_WEBHOOK_EVENTS = (
    "ARRAY['conversation.created', 'conversation.updated', "
    "'conversation.status_changed', 'message.created', 'message.updated', "
    "'contact.created', 'contact.updated']"
)


class EventOutbox(Base):
    """Domain events written in the same transaction as the change they describe."""

    __tablename__ = "event_outbox"
    __table_args__ = (
        Index(
            "event_outbox_pending_idx",
            "available_at",
            "id",
            postgresql_where=text("state = 'pending'"),
        ),
        Index("event_outbox_aggregate_idx", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[OutboxState] = mapped_column(
        OUTBOX_STATE, nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    available_at: Mapped[datetime] = created_at_col()
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class InboundWebhookEvent(Base):
    """Raw provider webhooks. The (provider, dedup_key) unique key prevents double processing."""

    __tablename__ = "inbound_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "provider", "dedup_key", name="inbound_webhook_events_dedup_uniq"
        ),
        Index(
            "inbound_webhook_events_state_idx",
            "state",
            "received_at",
            postgresql_where=text("state IN ('received', 'failed')"),
        ),
        Index(
            "inbound_webhook_events_org_idx",
            "organization_id",
            text("received_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[ChannelProvider] = mapped_column(CHANNEL_PROVIDER, nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE")
    )
    inbox_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[InboundEventState] = mapped_column(
        INBOUND_EVENT_STATE, nullable=False, server_default=text("'received'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = created_at_col()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "url", name="webhook_subscriptions_url_uniq"),
        CheckConstraint("url ~ '^https://'", name="webhook_subscriptions_https_chk"),
        UniqueConstraint("workspace_id", "id", name="webhook_subscriptions_ws_id_uniq"),
        workspace_fk(),
        ws_fk("inbox_id", "inboxes"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    inbox_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    name: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    events: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text(DEFAULT_WEBHOOK_EVENTS)
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "outbox_event_id", name="webhook_deliveries_event_uniq"
        ),
        ws_fk("subscription_id", "webhook_subscriptions"),
        ForeignKeyConstraint(
            ["outbox_event_id"], ["event_outbox.id"], ondelete="SET NULL"
        ),
        workspace_fk(),
        Index(
            "webhook_deliveries_retry_idx",
            "next_attempt_at",
            postgresql_where=text("state IN ('pending', 'failed')"),
        ),
        Index("webhook_deliveries_sub_idx", "subscription_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    outbox_event_id: Mapped[int | None] = mapped_column(BigInteger)
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[WebhookDeliveryState] = mapped_column(
        WEBHOOK_DELIVERY_STATE, nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body_excerpt: Mapped[str | None] = mapped_column(String(2000))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "idempotency_key"),
        Index("idempotency_keys_expiry_idx", "expires_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
