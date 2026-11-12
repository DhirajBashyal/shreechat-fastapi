"""Audit log writes for security events (docs/10_AUTHENTICATION_AND_RBAC.md §8)."""

import ipaddress
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger import request_ctx_var
from app.models import AuditLog


def client_ip(request: Request | None) -> str | None:
    """Client IP for inet columns; None when absent or not a valid address."""
    if request is None or request.client is None:
        return None
    try:
        return str(ipaddress.ip_address(request.client.host))
    except ValueError:
        return None


def audit(
    session: AsyncSession,
    *,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    actor_kind: str = "user",
    organization_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    changes: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    """Add an audit row to the session; it is committed with the caller's
    transaction, so the log and the change succeed or fail together."""
    ctx = request_ctx_var.get()
    session.add(
        AuditLog(
            organization_id=organization_id,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            changes=changes or {},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent") if request else None,
            request_id=ctx.request_id if ctx else None,
        )
    )
