"""Messaging windows (MESSAGE_PROCESSING.md §3)."""

from datetime import UTC, datetime, timedelta

from app.channels.base import WindowClosedError

STANDARD = timedelta(hours=24)
HUMAN_AGENT = timedelta(days=7)


def _age(last_incoming_at: datetime | None, now: datetime | None) -> timedelta | None:
    if last_incoming_at is None:
        return None
    now = now or datetime.now(UTC)
    if last_incoming_at.tzinfo is None:
        last_incoming_at = last_incoming_at.replace(tzinfo=UTC)
    return now - last_incoming_at


def whatsapp_free_form_allowed(
    last_incoming_at: datetime | None, now: datetime | None = None
) -> bool:
    age = _age(last_incoming_at, now)
    return age is not None and age <= STANDARD


def messaging_type(
    last_incoming_at: datetime | None, sender_kind: str, now: datetime | None = None
) -> str:
    """Messenger / Instagram: "RESPONSE" within 24 h; "HUMAN_AGENT" for a human
    agent within 7 days; otherwise WindowClosedError (AI/automation included)."""
    age = _age(last_incoming_at, now)
    if age is not None and age <= STANDARD:
        return "RESPONSE"
    if age is not None and age <= HUMAN_AGENT and sender_kind == "user":
        return "HUMAN_AGENT"
    raise WindowClosedError("Outside the messaging window")
