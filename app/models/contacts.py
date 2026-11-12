"""Contacts, their per-inbox identities, notes and labels."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    CIText,
    created_at_col,
    deleted_at_col,
    jsonb_object,
    org_fk,
    random_token_default,
    updated_at_col,
    uuid_pk,
    workspace_fk,
    ws_fk,
)


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="contacts_ws_id_uniq"),
        CheckConstraint(
            "contact_type IN ('visitor', 'lead', 'customer')", name="contacts_type_chk"
        ),
        CheckConstraint(
            "jsonb_typeof(custom_attributes) = 'object'",
            name="contacts_custom_attrs_is_object",
        ),
        CheckConstraint(
            "jsonb_typeof(additional_attributes) = 'object'",
            name="contacts_additional_attrs_is_object",
        ),
        workspace_fk(),
        org_fk("avatar_file_id", "stored_files", ondelete="SET NULL"),
        Index(
            "contacts_email_per_ws_uniq",
            "workspace_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index(
            "contacts_identifier_per_ws_uniq",
            "workspace_id",
            "identifier",
            unique=True,
            postgresql_where=text("identifier IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index(
            "contacts_phone_idx",
            "workspace_id",
            "phone_number",
            postgresql_where=text("phone_number IS NOT NULL"),
        ),
        Index(
            "contacts_activity_idx",
            "workspace_id",
            text("last_activity_at DESC NULLS LAST"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "contacts_search_trgm_idx",
            text(
                "(coalesce(name, '') || ' ' || coalesce(email::text, '') || ' ' "
                "|| coalesce(phone_number, '')) gin_trgm_ops"
            ),
            postgresql_using="gin",
        ),
        Index("contacts_org_idx", "organization_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("''")
    )
    email: Mapped[str | None] = mapped_column(CIText())
    phone_number: Mapped[str | None] = mapped_column(String(32))
    identifier: Mapped[str | None] = mapped_column(String(255))
    avatar_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    contact_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'visitor'")
    )
    blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    country_code: Mapped[str | None] = mapped_column(String(2))
    location: Mapped[str | None] = mapped_column(String(255))
    additional_attributes: Mapped[dict[str, Any]] = jsonb_object()
    custom_attributes: Mapped[dict[str, Any]] = jsonb_object()
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = deleted_at_col()


class ContactIdentity(Base):
    """A contact's identity on one inbox (PSID, IGSID, wa_id, widget visitor id)."""

    __tablename__ = "contact_identities"
    __table_args__ = (
        UniqueConstraint(
            "inbox_id", "source_id", name="contact_identities_source_uniq"
        ),
        UniqueConstraint("workspace_id", "id", name="contact_identities_ws_id_uniq"),
        UniqueConstraint("realtime_token", name="contact_identities_token_uniq"),
        ws_fk("contact_id", "contacts"),
        ws_fk("inbox_id", "inboxes"),
        workspace_fk(),
        Index("contact_identities_contact_idx", "contact_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    inbox_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    realtime_token: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=random_token_default()
    )
    identity_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    profile: Mapped[dict[str, Any]] = jsonb_object()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Note(Base):
    """A note on a contact. Private conversation notes are messages with is_private."""

    __tablename__ = "notes"
    __table_args__ = (
        ws_fk("contact_id", "contacts"),
        workspace_fk(),
        Index(
            "notes_contact_idx",
            "contact_id",
            text("created_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = deleted_at_col()


class ContactLabel(Base):
    __tablename__ = "contact_labels"
    __table_args__ = (
        ws_fk("contact_id", "contacts"),
        ws_fk("label_id", "labels"),
        workspace_fk(),
        Index("contact_labels_label_idx", "label_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    label_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = created_at_col()
