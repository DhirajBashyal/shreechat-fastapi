"""Messenger / Instagram webhook parsing (FACEBOOK_MESSENGER.md §4, INSTAGRAM.md §4).

Both use the Messenger Platform shape: ``entry[].messaging[]`` items with
``sender``, ``recipient``, ``timestamp`` and one of ``message``, ``postback``,
``delivery``, ``read``, ``reaction``, ``referral``. Pure functions.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from app.channels.types import (
    DeletionEvent,
    DeliveryUpdate,
    InboundAttachment,
    InboundEvent,
    InboundMessage,
    ReactionEvent,
    ReferralEvent,
    RouteKey,
    SenderProfile,
)

MEDIA = {"image": "image", "video": "video", "audio": "audio", "file": "file"}


def _ms(value: Any) -> datetime:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return datetime.now(UTC)
    if v > 10**11:  # milliseconds
        v //= 1000
    return datetime.fromtimestamp(v, tz=UTC)


def _event_type(item: dict[str, Any]) -> str:
    message = item.get("message")
    if isinstance(message, dict):
        if message.get("is_echo"):
            return "echo"
        if message.get("is_deleted"):
            return "deletion"
        return "message"
    for key in ("postback", "delivery", "read", "reaction", "referral"):
        if key in item:
            return key
    return "other"


def route_keys(payload: dict[str, Any], *, obj: str) -> list[RouteKey]:
    if payload.get("object") != obj:
        return []
    units: list[RouteKey] = []
    for entry in payload.get("entry") or []:
        routing_id = str(entry.get("id", ""))
        for item in entry.get("messaging") or []:
            kind = _event_type(item)
            if kind == "other":
                continue
            sender = str((item.get("sender") or {}).get("id", ""))
            recipient = str((item.get("recipient") or {}).get("id", ""))
            customer = recipient if kind == "echo" else sender
            message = item.get("message") or {}
            if kind in ("message", "echo"):
                dedup = str(message.get("mid"))
                if kind == "echo":
                    dedup = f"echo:{dedup}"
            elif kind == "deletion":
                dedup = f"deleted:{message.get('mid')}"
            elif kind == "postback":
                dedup = str(item["postback"].get("mid") or _digest(item))
            elif kind == "delivery":
                dedup = f"{sender}:{item['delivery'].get('watermark')}:delivery"
            elif kind == "read":
                read = item["read"]
                dedup = f"{sender}:{read.get('watermark') or read.get('mid')}:read"
            elif kind == "reaction":
                r = item["reaction"]
                dedup = f"{sender}:{r.get('mid')}:{r.get('action')}:{r.get('emoji') or r.get('reaction')}:{item.get('timestamp')}"
            else:
                dedup = _digest(item)
            units.append(
                RouteKey(
                    routing_id=routing_id,
                    dedup_key=dedup,
                    ordering_key=customer,
                    event_type=kind,
                    payload={"item": item},
                )
            )
    return units


def _digest(item: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()


def _attachments(
    message: dict[str, Any], *, instagram: bool
) -> tuple[list[InboundAttachment], dict[str, Any], str]:
    out: list[InboundAttachment] = []
    attrs: dict[str, Any] = {}
    content_type = "text"
    sticker = message.get("sticker_id")
    for a in message.get("attachments") or []:
        kind = a.get("type")
        payload = a.get("payload") or {}
        if sticker and kind == "image":
            out.append(InboundAttachment(kind="sticker", url=payload.get("url")))
        elif kind in MEDIA:
            out.append(InboundAttachment(kind=MEDIA[kind], url=payload.get("url")))  # type: ignore[arg-type]
        elif kind == "location":
            coords = payload.get("coordinates") or {}
            content_type = "location"
            out.append(
                InboundAttachment(
                    kind="location",
                    latitude=coords.get("lat"),
                    longitude=coords.get("long"),
                    title=a.get("title"),
                )
            )
        elif instagram and kind == "share":
            out.append(
                InboundAttachment(
                    kind="fallback", url=payload.get("url"), title="Shared post"
                )
            )
            attrs["share"] = payload
        elif instagram and kind == "story_mention":
            out.append(
                InboundAttachment(
                    kind="fallback", url=payload.get("url"), title="Story mention"
                )
            )
            attrs["story"] = {"type": "mention", "url": payload.get("url")}
        elif kind == "fallback":
            out.append(
                InboundAttachment(
                    kind="fallback",
                    url=payload.get("url") or a.get("url"),
                    title=a.get("title"),
                )
            )
        else:
            out.append(
                InboundAttachment(kind="fallback", url=payload.get("url"), title=kind)
            )
            attrs.setdefault("unsupported_attachments", []).append(kind)
    return out, attrs, content_type


def parse_item(item: dict[str, Any], *, instagram: bool) -> list[InboundEvent]:
    kind = _event_type(item)
    when = _ms(item.get("timestamp"))
    sender_id = str((item.get("sender") or {}).get("id", ""))
    recipient_id = str((item.get("recipient") or {}).get("id", ""))
    if kind in ("message", "echo"):
        message = item["message"]
        echo = kind == "echo"
        customer = SenderProfile(source_id=recipient_id if echo else sender_id)
        attachments, attrs, content_type = _attachments(message, instagram=instagram)
        text = message.get("text")
        if message.get("is_unsupported"):
            content_type = "unsupported"
            attrs["unsupported"] = {"type": "instagram_unsupported"}
        quick = message.get("quick_reply") or {}
        if quick.get("payload"):
            content_type = "interactive"
            attrs["interactive_reply"] = {"payload": quick["payload"], "title": text}
        reply_to = message.get("reply_to") or {}
        if instagram and reply_to.get("story"):
            story = reply_to["story"]
            attrs["story"] = {
                "type": "reply",
                "url": story.get("url"),
                "id": story.get("id"),
            }
        if echo and message.get("app_id"):
            attrs["app_id"] = message.get("app_id")
        attrs["provider_sent_at"] = when.isoformat()
        return [
            InboundMessage(
                external_id=str(message.get("mid")),
                sender=customer,
                direction="outgoing_echo" if echo else "incoming",
                sent_at=when,
                text=text,
                content_type=content_type,
                attachments=attachments,
                in_reply_to_external_id=reply_to.get("mid"),
                echo_metadata=message.get("metadata") if echo else None,
                content_attributes=attrs,
            )
        ]
    if kind == "deletion":
        return [
            DeletionEvent(
                sender=SenderProfile(source_id=sender_id),
                external_id=str(item["message"].get("mid")),
                occurred_at=when,
            )
        ]
    if kind == "postback":
        pb = item["postback"]
        return [
            InboundMessage(
                external_id=str(pb.get("mid") or f"postback:{_digest(item)[:40]}"),
                sender=SenderProfile(source_id=sender_id),
                sent_at=when,
                text=pb.get("title"),
                content_type="interactive",
                content_attributes={
                    "postback": pb.get("payload"),
                    "interactive_reply": {
                        "payload": pb.get("payload"),
                        "title": pb.get("title"),
                    },
                    "provider_sent_at": when.isoformat(),
                },
                conversation_attributes={"meta_referral": pb["referral"]}
                if pb.get("referral")
                else {},
            )
        ]
    if kind == "delivery":
        d = item["delivery"]
        return [
            DeliveryUpdate(
                status="delivered",
                watermark=_ms(d.get("watermark")) if d.get("watermark") else None,
                external_id=None,
                recipient_source_id=sender_id,
                occurred_at=when,
            )
        ]
    if kind == "read":
        r = item["read"]
        return [
            DeliveryUpdate(
                status="read",
                watermark=_ms(r.get("watermark")) if r.get("watermark") else None,
                external_id=r.get("mid") if not r.get("watermark") else None,
                recipient_source_id=sender_id,
                occurred_at=when,
            )
        ]
    if kind == "reaction":
        r = item["reaction"]
        emoji = (
            None
            if r.get("action") == "unreact"
            else (r.get("emoji") or r.get("reaction"))
        )
        return [
            ReactionEvent(
                sender=SenderProfile(source_id=sender_id),
                target_external_id=str(r.get("mid", "")),
                emoji=emoji,
                occurred_at=when,
            )
        ]
    if kind == "referral":
        return [
            ReferralEvent(
                sender=SenderProfile(source_id=sender_id),
                referral=item["referral"],
                occurred_at=when,
            )
        ]
    return []
