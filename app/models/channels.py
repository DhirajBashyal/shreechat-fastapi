"""Inboxes, channel connections and per-provider channel details."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    created_at_col,
    deleted_at_col,
    jsonb_array,
    jsonb_object,
    org_fk,
    random_token_default,
    updated_at_col,
    uuid_pk,
    workspace_fk,
    ws_fk,
)
from app.models.enums import (
    CHANNEL_PROVIDER,
    CONNECTION_STATUS,
    ChannelProvider,
    ConnectionStatus,
)


class Inbox(Base):
    __tablename__ = "inboxes"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="inboxes_ws_id_uniq"),
        CheckConstraint(
            "jsonb_typeof(working_hours) = 'array'",
            name="inboxes_working_hours_is_array",
        ),
        workspace_fk(),
        Index(
            "inboxes_ws_idx",
            "workspace_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("inboxes_org_idx", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[ChannelProvider] = mapped_column(CHANNEL_PROVIDER, nullable=False)
    enable_auto_assignment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    auto_assignment_config: Mapped[dict[str, Any]] = jsonb_object()
    greeting_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    greeting_message: Mapped[str | None] = mapped_column(Text)
    working_hours_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    working_hours: Mapped[list[Any]] = jsonb_array()
    out_of_office_message: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'UTC'")
    )
    allow_messages_after_resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    lock_to_single_conversation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    settings: Mapped[dict[str, Any]] = jsonb_object()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = deleted_at_col()


class InboxMember(Base):
    __tablename__ = "inbox_members"
    __table_args__ = (
        ws_fk("inbox_id", "inboxes"),
        workspace_fk(),
        Index("inbox_members_user_idx", "user_id", "workspace_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    inbox_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = created_at_col()


class ChannelConnection(Base):
    """One connection per inbox: provider-agnostic state + encrypted secrets."""

    __tablename__ = "channel_connections"
    __table_args__ = (
        UniqueConstraint("inbox_id", name="channel_connections_inbox_uniq"),
        UniqueConstraint("workspace_id", "id", name="channel_connections_ws_id_uniq"),
        CheckConstraint(
            "credentials_ciphertext IS NULL OR credentials_key_version IS NOT NULL",
            name="channel_connections_secret_key_chk",
        ),
        ws_fk("inbox_id", "inboxes"),
        workspace_fk(),
        Index(
            "channel_connections_token_expiry_idx",
            "token_expires_at",
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    inbox_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider: Mapped[ChannelProvider] = mapped_column(CHANNEL_PROVIDER, nullable=False)
    status: Mapped[ConnectionStatus] = mapped_column(
        CONNECTION_STATUS, nullable=False, server_default=text("'pending'")
    )
    credentials_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    credentials_key_version: Mapped[int | None] = mapped_column(SmallInteger)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config: Mapped[dict[str, Any]] = jsonb_object()
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ChannelRoute(Base):
    """Global routing table for webhook ingress, read before the tenant is known.

    Maps (provider, external id) → tenant/inbox. Not covered by RLS.
    """

    __tablename__ = "channel_routes"
    __table_args__ = (
        PrimaryKeyConstraint("provider", "external_id"),
        workspace_fk(),
        Index("channel_routes_connection_idx", "channel_connection_id"),
    )

    provider: Mapped[ChannelProvider] = mapped_column(CHANNEL_PROVIDER)
    external_id: Mapped[str] = mapped_column(String(255))
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    inbox_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("channel_connections.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = created_at_col()


class FacebookPage(Base):
    __tablename__ = "facebook_pages"
    __table_args__ = (
        UniqueConstraint(
            "channel_connection_id", name="facebook_pages_connection_uniq"
        ),
        UniqueConstraint(
            "organization_id", "page_id", name="facebook_pages_page_per_org_uniq"
        ),
        ws_fk("channel_connection_id", "channel_connections"),
        workspace_fk(),
        Index("facebook_pages_page_idx", "page_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    page_name: Mapped[str | None] = mapped_column(String(255))
    linked_instagram_id: Mapped[str | None] = mapped_column(String(64))
    subscribed_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class InstagramAccount(Base):
    __tablename__ = "instagram_accounts"
    __table_args__ = (
        UniqueConstraint(
            "channel_connection_id", name="instagram_accounts_connection_uniq"
        ),
        UniqueConstraint("instagram_user_id", name="instagram_accounts_ig_uniq"),
        CheckConstraint(
            "connection_method IN ('instagram_login', 'facebook_page')",
            name="instagram_accounts_method_chk",
        ),
        ws_fk("channel_connection_id", "channel_connections"),
        workspace_fk(),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    instagram_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    connection_method: Mapped[str] = mapped_column(String(32), nullable=False)
    facebook_page_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class WhatsappBusinessAccount(Base):
    __tablename__ = "whatsapp_business_accounts"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "waba_id", name="whatsapp_waba_per_org_uniq"
        ),
        UniqueConstraint("organization_id", "id", name="whatsapp_waba_org_id_uniq"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    waba_id: Mapped[str] = mapped_column(String(64), nullable=False)
    business_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(255))
    credentials_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    credentials_key_version: Mapped[int | None] = mapped_column(SmallInteger)
    templates_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class WhatsappPhoneNumber(Base):
    __tablename__ = "whatsapp_phone_numbers"
    __table_args__ = (
        UniqueConstraint(
            "channel_connection_id", name="whatsapp_phone_connection_uniq"
        ),
        UniqueConstraint("phone_number_id", name="whatsapp_phone_number_id_uniq"),
        ws_fk("channel_connection_id", "channel_connections"),
        org_fk("waba_id", "whatsapp_business_accounts"),
        workspace_fk(),
        Index("whatsapp_phone_waba_idx", "waba_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    waba_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    phone_number_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    verified_name: Mapped[str | None] = mapped_column(String(255))
    quality_rating: Mapped[str | None] = mapped_column(String(16))
    messaging_limit_tier: Mapped[str | None] = mapped_column(String(32))
    health: Mapped[dict[str, Any]] = jsonb_object()
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class WhatsappMessageTemplate(Base):
    __tablename__ = "whatsapp_message_templates"
    __table_args__ = (
        UniqueConstraint("waba_id", "name", "language", name="whatsapp_templates_uniq"),
        CheckConstraint(
            "jsonb_typeof(components) = 'array'",
            name="whatsapp_templates_components_is_array",
        ),
        ForeignKeyConstraint(
            ["organization_id", "waba_id"],
            [
                "whatsapp_business_accounts.organization_id",
                "whatsapp_business_accounts.id",
            ],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    waba_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    external_template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    components: Mapped[list[Any]] = jsonb_array()
    parameter_format: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'positional'")
    )
    synced_at: Mapped[datetime] = created_at_col()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class WebWidget(Base):
    __tablename__ = "web_widgets"
    __table_args__ = (
        UniqueConstraint("channel_connection_id", name="web_widgets_connection_uniq"),
        UniqueConstraint("website_token", name="web_widgets_token_uniq"),
        ws_fk("channel_connection_id", "channel_connections"),
        workspace_fk(),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    website_token: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=random_token_default()
    )
    website_url: Mapped[str | None] = mapped_column(Text)
    allowed_origins: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    widget_color: Mapped[str] = mapped_column(
        String(9), nullable=False, server_default=text("'#1f93ff'")
    )
    welcome_title: Mapped[str | None] = mapped_column(String(255))
    welcome_tagline: Mapped[str | None] = mapped_column(String(255))
    reply_time: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'in_a_few_minutes'")
    )
    pre_chat_form_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    pre_chat_form_options: Mapped[dict[str, Any]] = jsonb_object()
    identity_hmac_secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    identity_hmac_key_version: Mapped[int | None] = mapped_column(SmallInteger)
    identity_verification_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    feature_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("ARRAY['attachments', 'emoji_picker', 'end_conversation']"),
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
