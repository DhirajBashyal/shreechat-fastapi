"""Reporting events, subscriptions and billing, usage metering, audit log."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    created_at_col,
    jsonb_array,
    jsonb_object,
    updated_at_col,
    uuid_pk,
    workspace_fk,
)
from app.models.enums import (
    BILLING_RECORD_STATE,
    SUBSCRIPTION_STATE,
    BillingRecordState,
    SubscriptionState,
)


class ReportingEvent(Base):
    __tablename__ = "reporting_events"
    __table_args__ = (
        CheckConstraint(
            "name IN ('first_response', 'reply_time', 'conversation_resolved', "
            "'conversation_opened', 'conversation_bot_handoff', 'conversation_bot_resolved')",
            name="reporting_events_name_chk",
        ),
        workspace_fk(),
        Index(
            "reporting_events_ws_name_time_idx", "workspace_id", "name", "created_at"
        ),
        Index(
            "reporting_events_ws_inbox_idx", "workspace_id", "inbox_id", "created_at"
        ),
        Index("reporting_events_ws_user_idx", "workspace_id", "user_id", "created_at"),
        Index("reporting_events_conversation_idx", "conversation_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    inbox_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    value_seconds: Mapped[float] = mapped_column(Double, nullable=False)
    value_in_business_hours_seconds: Mapped[float | None] = mapped_column(Double)
    event_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    event_end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = created_at_col()


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint("seats >= 1", name="subscriptions_seats_chk"),
        CheckConstraint(
            "current_period_end > current_period_start", name="subscriptions_period_chk"
        ),
        Index(
            "subscriptions_one_live_per_org",
            "organization_id",
            unique=True,
            postgresql_where=text(
                "state IN ('trialing', 'active', 'past_due', 'incomplete')"
            ),
        ),
        Index(
            "subscriptions_external_uniq",
            "external_subscription_id",
            unique=True,
            postgresql_where=text("external_subscription_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("subscription_plans.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[SubscriptionState] = mapped_column(SUBSCRIPTION_STATE, nullable=False)
    seats: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    external_subscription_id: Mapped[str | None] = mapped_column(String(255))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    current_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class BillingRecord(Base):
    __tablename__ = "billing_records"
    __table_args__ = (
        CheckConstraint(
            "amount_due_cents >= 0 AND amount_paid_cents >= 0",
            name="billing_records_amount_chk",
        ),
        Index(
            "billing_records_external_uniq",
            "external_invoice_id",
            unique=True,
            postgresql_where=text("external_invoice_id IS NOT NULL"),
        ),
        Index("billing_records_org_idx", "organization_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("subscriptions.id", ondelete="SET NULL")
    )
    external_invoice_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[BillingRecordState] = mapped_column(
        BILLING_RECORD_STATE, nullable=False
    )
    amount_due_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_paid_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hosted_invoice_url: Mapped[str | None] = mapped_column(Text)
    line_items: Mapped[list[Any]] = jsonb_array()
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (
        CheckConstraint(
            "metric IN ('ai_responses', 'ai_tokens', 'conversations', 'storage_bytes')",
            name="usage_records_metric_chk",
        ),
        UniqueConstraint(
            "organization_id", "metric", "source_id", name="usage_records_source_uniq"
        ),
        Index("usage_records_period_idx", "organization_id", "metric", "period_start"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class BillingWebhookEvent(Base):
    """Billing provider (Stripe) webhooks, deduplicated by the provider's event id."""

    __tablename__ = "billing_webhook_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = created_at_col()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "actor_kind IN ('user', 'platform_admin', 'api_token', 'system', 'ai_agent')",
            name="audit_logs_actor_chk",
        ),
        Index("audit_logs_org_time_idx", "organization_id", text("created_at DESC")),
        Index("audit_logs_resource_idx", "resource_type", "resource_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE")
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    changes: Mapped[dict[str, Any]] = jsonb_object()
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_col()
