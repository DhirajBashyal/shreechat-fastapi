"""Stored file metadata (the bytes live in local disk or S3)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, jsonb_object, uuid_pk


class StoredFile(Base):
    __tablename__ = "stored_files"
    __table_args__ = (
        UniqueConstraint(
            "storage_backend", "storage_key", name="stored_files_key_uniq"
        ),
        UniqueConstraint("organization_id", "id", name="stored_files_org_id_uniq"),
        CheckConstraint("byte_size >= 0", name="stored_files_size_chk"),
        CheckConstraint(
            "storage_backend IN ('local', 's3')", name="stored_files_backend_chk"
        ),
        CheckConstraint(
            "scan_status IN ('pending', 'clean', 'infected', 'skipped')",
            name="stored_files_scan_chk",
        ),
        Index("stored_files_org_created_idx", "organization_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # Column "metadata"; the attribute is renamed because declarative reserves it.
    file_metadata: Mapped[dict[str, Any]] = jsonb_object("metadata")
    scan_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    created_at: Mapped[datetime] = created_at_col()
