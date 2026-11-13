"""WhatsApp webhook → normalised events (WHATSAPP.md §4). Pure functions.

Unknown message types become ``unsupported`` messages, never exceptions.
"""

from datetime import UTC, datetime
from typing import Any

from app.channels.types import (
    DeliveryUpdate,
    InboundAttachment,
    InboundEvent,
    InboundMessage,
    ReactionEvent,
    RouteKey,
    SenderProfile,
)

MEDIA_TYPES = {
    "image": "image",
    "video": "video",
    "document": "file",
    "sticker": "sticker",
    "audio": "audio",
}
STATUSES = ("sent", "delivered", "read", "failed")


def _ts(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def route_keys(payload: dict[str, Any]) -> list[RouteKey]:
    if payload.get("object") != "whatsapp_business_account":
        return []
    units: list[RouteKey] = []
    for entry in payload.get("entry") or []:
        waba_id = str(entry.get("id", ""))
        for change in entry.get("changes") or []:
            field = change.get("field")
            value = change.get("value") or {}
            if field == "message_template_status_update":
                units.append(
                    RouteKey(
                        routing_id=f"waba:{waba_id}",
                        dedup_key=(
                            f"tpl:{value.get('message_template_id')}:{value.get('event')}:"
                            f"{entry.get('time') or value.get('timestamp') or ''}"
                        ),
                        ordering_key=f"waba:{waba_id}",
                        event_type="template_status",
                        payload={"field": field, "waba_id": waba_id, "value": value},
                    )
                )
                continue
            if field not in ("messages", "smb_message_echoes"):
                continue  # tracking_events etc.
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id", ""))
            if not phone_number_id:
                continue
            contacts = value.get("contacts") or []
            for message in value.get("messages") or []:
                units.append(
                    RouteKey(
                        routing_id=phone_number_id,
                        dedup_key=str(message.get("id")),
                        ordering_key=_sender_ids(message, contacts)[0],
                        event_type="message",
                        payload={
                            "field": field,
                            "metadata": metadata,
                            "contacts": contacts,
                            "message": message,
                        },
                    )
                )
            for echo in value.get("message_echoes") or []:
                units.append(
                    RouteKey(
                        routing_id=phone_number_id,
                        dedup_key=f"echo:{echo.get('id')}",
                        ordering_key=str(echo.get("to", "")),
                        event_type="echo",
                        payload={"field": field, "metadata": metadata, "message": echo},
                    )
                )
            for status in value.get("statuses") or []:
                code = ""
                if status.get("status") == "failed":
                    errors = status.get("errors") or [{}]
                    code = f":{errors[0].get('code', '')}"
                units.append(
                    RouteKey(
                        routing_id=phone_number_id,
                        dedup_key=f"{status.get('id')}:{status.get('status')}{code}",
                        ordering_key=str(status.get("recipient_id", "")),
                        event_type="status",
                        payload={
                            "field": field,
                            "metadata": metadata,
                            "status": status,
                        },
                    )
                )
    return units


def _sender_ids(message: dict[str, Any], contacts: list[dict[str, Any]]) -> list[str]:
    """[source_id, *alternates]: parent BSUID > BSUID > wa_id (§4 Identity)."""
    wa_id = str(message.get("from") or "")
    contact = next(
        (c for c in contacts if str(c.get("wa_id", "")) == wa_id),
        contacts[0] if contacts else {},
    )
    candidates = [
        message.get("from_parent_user_id"),
        contact.get("parent_user_id"),
        message.get("from_user_id"),
        contact.get("user_id"),
        wa_id or contact.get("wa_id"),
    ]
    out: list[str] = []
    for c in candidates:
        if c and str(c) not in out:
            out.append(str(c))
    return out or [""]


def _sender(message: dict[str, Any], contacts: list[dict[str, Any]]) -> SenderProfile:
    ids = _sender_ids(message, contacts)
    wa_id = str(message.get("from") or "")
    contact = next(
        (c for c in contacts if str(c.get("wa_id", "")) == wa_id),
        contacts[0] if contacts else {},
    )
    return SenderProfile(
        source_id=ids[0],
        alternate_source_ids=ids[1:],
        name=(contact.get("profile") or {}).get("name"),
        phone_number=f"+{wa_id}" if wa_id.isdigit() else None,
    )


def parse_message(
    message: dict[str, Any], contacts: list[dict[str, Any]], *, echo: bool = False
) -> list[InboundEvent]:
    kind = message.get("type") or "unsupported"
    when = _ts(message.get("timestamp"))
    if echo:
        to = str(message.get("to") or "")
        sender = SenderProfile(
            source_id=to, phone_number=f"+{to}" if to.isdigit() else None
        )
    else:
        sender = _sender(message, contacts)
    if kind == "reaction":
        reaction = message.get("reaction") or {}
        return [
            ReactionEvent(
                sender=sender,
                target_external_id=str(reaction.get("message_id", "")),
                emoji=reaction.get("emoji") or None,
                occurred_at=when,
            )
        ]
    text: str | None = None
    content_type = "text"
    attrs: dict[str, Any] = {}
    attachments: list[InboundAttachment] = []
    if kind == "text":
        text = (message.get("text") or {}).get("body")
    elif kind in MEDIA_TYPES:
        media = message.get(kind) or {}
        text = media.get("caption")
        attachments.append(
            InboundAttachment(
                kind=MEDIA_TYPES[kind],  # type: ignore[arg-type]
                provider_media_id=media.get("id"),
                mime_type=media.get("mime_type"),
                sha256=media.get("sha256"),
                filename=media.get("filename"),
                is_voice=bool(media.get("voice")) if kind == "audio" else False,
            )
        )
    elif kind == "location":
        loc = message.get("location") or {}
        content_type = "location"
        attachments.append(
            InboundAttachment(
                kind="location",
                latitude=loc.get("latitude"),
                longitude=loc.get("longitude"),
                title=loc.get("name") or loc.get("address"),
            )
        )
        attrs["location"] = {
            k: loc.get(k) for k in ("name", "address", "url") if loc.get(k)
        }
    elif kind == "contacts":
        content_type = "contact_card"
        attrs["contacts"] = message.get("contacts") or []
        names = [
            ((c.get("name") or {}).get("formatted_name") or "")
            for c in attrs["contacts"]
        ]
        text = ", ".join(n for n in names if n) or None
    elif kind == "interactive":
        inter = message.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        content_type = "interactive"
        text = reply.get("title")
        attrs["interactive_reply"] = {
            "id": reply.get("id"),
            "title": reply.get("title"),
            "type": inter.get("type"),
        }
    elif kind == "button":
        button = message.get("button") or {}
        content_type = "interactive"
        text = button.get("text")
        attrs["interactive_reply"] = {
            "payload": button.get("payload"),
            "title": button.get("text"),
        }
    else:
        content_type = "unsupported"
        errors = message.get("errors") or []
        detail = errors[0].get("title") or errors[0].get("message") if errors else None
        attrs["unsupported"] = {"type": kind, "error": detail}
        if kind == "system":
            attrs["system"] = message.get("system") or {}
    context = message.get("context") or {}
    if context.get("forwarded") or context.get("frequently_forwarded"):
        attrs["forwarded"] = True
    conv_attrs: dict[str, Any] = {}
    if message.get("referral"):
        conv_attrs["meta_referral"] = message["referral"]
    attrs["provider_sent_at"] = when.isoformat()
    return [
        InboundMessage(
            external_id=str(message.get("id")),
            sender=sender,
            direction="outgoing_echo" if echo else "incoming",
            sent_at=when,
            text=text,
            content_type=content_type,
            attachments=attachments,
            in_reply_to_external_id=context.get("id")
            if not context.get("forwarded")
            else None,
            content_attributes=attrs,
            conversation_attributes=conv_attrs,
        )
    ]


def parse_status(status: dict[str, Any]) -> list[InboundEvent]:
    value = status.get("status")
    if value not in STATUSES:
        return []
    code = message = None
    if value == "failed":
        err = (status.get("errors") or [{}])[0]
        code = str(err.get("code", "")) or None
        message = err.get("title") or err.get("message")
    billing = None
    if status.get("pricing") or status.get("conversation"):
        billing = {
            "pricing": status.get("pricing"),
            "conversation": status.get("conversation"),
        }
    return [
        DeliveryUpdate(
            status=value,
            external_id=str(status.get("id")),
            recipient_source_id=str(status.get("recipient_id", "")),
            error_code=code,
            error_message=message,
            occurred_at=_ts(status.get("timestamp")),
            billing=billing,
        )
    ]


def parse_unit(unit: dict[str, Any]) -> list[InboundEvent]:
    if "status" in unit:
        return parse_status(unit["status"])
    if "message" in unit:
        return parse_message(
            unit["message"],
            unit.get("contacts") or [],
            echo=unit.get("field") == "smb_message_echoes",
        )
    return []
