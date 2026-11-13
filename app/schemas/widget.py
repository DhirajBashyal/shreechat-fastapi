"""Website live chat schemas (docs/integrations/WEBSITE_CHAT.md).

Visitor side: /api/v1/widget/*. Admin side: inbox settings under
/api/v1/workspaces/{ws}/inboxes/.../web-widget.
"""

import re
import uuid
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

FeatureFlag = Literal["attachments", "emoji_picker", "end_conversation"]
ReplyTime = Literal["in_a_few_minutes", "in_a_few_hours", "in_a_day"]

_ORIGIN = re.compile(r"^https?://(\*\.)?[a-z0-9.-]+(:\d{1,5})?$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
_HOURS = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# =============================================================================
# Admin: inbox settings
# =============================================================================


class WorkingHoursEntry(_Strict):
    day: int = Field(ge=0, le=7, description="ISO weekday, 1 = Monday … 7 = Sunday")
    open: str = "09:00"
    close: str = "17:00"
    closed: bool = False

    @field_validator("open", "close")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HOURS.match(v):
            raise ValueError("use HH:MM")
        return v


class WebWidgetSettings(_Strict):
    """All optional: PATCH sends only what changed."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    website_url: str | None = Field(default=None, max_length=2048)
    allowed_origins: list[str] | None = Field(default=None, max_length=50)
    widget_color: str | None = None
    welcome_title: str | None = Field(default=None, max_length=255)
    welcome_tagline: str | None = Field(default=None, max_length=255)
    reply_time: ReplyTime | None = None
    pre_chat_form_enabled: bool | None = None
    pre_chat_form_options: dict[str, Any] | None = None
    identity_verification_required: bool | None = None
    feature_flags: list[FeatureFlag] | None = None
    greeting_enabled: bool | None = None
    greeting_message: str | None = Field(default=None, max_length=5000)
    working_hours_enabled: bool | None = None
    working_hours: list[WorkingHoursEntry] | None = Field(default=None, max_length=14)
    timezone: str | None = None
    out_of_office_message: str | None = Field(default=None, max_length=5000)
    allow_messages_after_resolved: bool | None = None
    lock_to_single_conversation: bool | None = None

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out: list[str] = []
        for origin in v:
            o = origin.strip().rstrip("/").lower()
            if not _ORIGIN.match(o):
                raise ValueError(
                    f"{origin!r} is not an origin (https://shop.example.com, "
                    "https://*.example.com)"
                )
            if o not in out:
                out.append(o)
        return out

    @field_validator("widget_color")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        if v is not None and not _COLOR.match(v):
            raise ValueError("use a hex colour like #1f93ff")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("unknown time zone") from exc
        return v

    @field_validator("pre_chat_form_options")
    @classmethod
    def _pre_chat(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        allowed = {"require_name", "require_email", "require_phone", "message"}
        if v is not None and set(v) - allowed:
            raise ValueError(f"allowed keys: {', '.join(sorted(allowed))}")
        return v


class WebWidgetCreate(WebWidgetSettings):
    name: str = Field(min_length=1, max_length=255)  # type: ignore[assignment]


class WebWidgetRead(BaseModel):
    inbox_id: uuid.UUID
    name: str
    status: str  # channel connection status: active | disconnected
    website_token: str
    embed_script: str
    website_url: str | None
    allowed_origins: list[str]
    widget_color: str
    welcome_title: str | None
    welcome_tagline: str | None
    reply_time: str
    pre_chat_form_enabled: bool
    pre_chat_form_options: dict[str, Any]
    identity_verification_required: bool
    identity_secret_set: bool
    feature_flags: list[str]
    greeting_enabled: bool
    greeting_message: str | None
    working_hours_enabled: bool
    working_hours: list[dict[str, Any]]
    timezone: str
    out_of_office_message: str | None
    allow_messages_after_resolved: bool
    lock_to_single_conversation: bool
    created_at: datetime


class WebWidgetCreated(WebWidgetRead):
    identity_secret: str = Field(description="Shown once. HMAC key for identify().")


class IdentitySecretResponse(BaseModel):
    identity_secret: str


# =============================================================================
# Visitor API
# =============================================================================


class WidgetConfig(BaseModel):
    website_token: str
    inbox_name: str
    widget_color: str
    welcome_title: str | None
    welcome_tagline: str | None
    reply_time: str
    pre_chat_form_enabled: bool
    pre_chat_form_options: dict[str, Any]
    feature_flags: list[str]
    identity_verification_required: bool
    working_hours_enabled: bool
    working_hours: list[dict[str, Any]]
    timezone: str
    is_online: bool  # within working hours (always true when hours are off)
    out_of_office_message: str | None
    agents_online: bool
    ai_assistant: bool  # an AI agent answers first: show the AI disclosure


class SessionCreate(_Strict):
    website_token: str | None = None  # also accepted as ?website_token=
    visitor_token: str | None = Field(default=None, max_length=4096)


class WidgetContact(BaseModel):
    name: str
    email: str | None
    phone_number: str | None
    identity_verified: bool


class SessionResponse(BaseModel):
    visitor_token: str
    realtime_token: str
    resumed: bool
    contact: WidgetContact


class IdentifyRequest(_Strict):
    website_token: str | None = None
    identifier: str = Field(min_length=1, max_length=255)
    identifier_hash: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    avatar_url: str | None = Field(default=None, max_length=2048)


class ContactUpdate(_Strict):
    website_token: str | None = None
    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    custom_attributes: dict[str, Any] | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("not an email address")
        return v.lower() if v else v


class WidgetSender(BaseModel):
    kind: str  # contact | user | ai_agent | system | automation
    name: str | None


class WidgetSource(BaseModel):
    title: str
    url: str | None


class WidgetMessage(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    direction: str
    content_type: str
    content: str | None
    content_attributes: dict[str, Any]
    sender: WidgetSender
    client_message_id: str | None
    sequence_number: int
    delivery_status: str
    sources: list[WidgetSource]
    attachments: list[dict[str, Any]]
    created_at: datetime


class WidgetConversation(BaseModel):
    id: uuid.UUID
    display_number: int
    status: str
    created_at: datetime
    last_activity_at: datetime
    contact_last_seen_at: datetime | None
    agent_last_seen_at: datetime | None
    unread_count: int
    custom_attributes: dict[str, Any]
    labels: list[str]
    last_message: WidgetMessage | None


class ConversationCreate(_Strict):
    website_token: str | None = None
    content: str = Field(min_length=1, max_length=20_000)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=64)
    contact: ContactUpdate | None = None  # pre-chat form
    custom_attributes: dict[str, Any] | None = None
    referer_url: str | None = Field(default=None, max_length=2048)
    browser_language: str | None = Field(default=None, max_length=35)


class ConversationCreated(BaseModel):
    conversation: WidgetConversation
    messages: list[WidgetMessage]


class MessageCreate(_Strict):
    website_token: str | None = None
    content: str = Field(min_length=1, max_length=20_000)
    client_message_id: str = Field(min_length=1, max_length=64)
    attachment_file_ids: list[uuid.UUID] = Field(default_factory=list)


class TypingRequest(_Strict):
    website_token: str | None = None
    state: Literal["on", "off"]


class ConversationAttributesUpdate(_Strict):
    website_token: str | None = None
    custom_attributes: dict[str, Any]


class LabelRequest(_Strict):
    website_token: str | None = None
    label: str = Field(min_length=1, max_length=100)


class EventCreate(_Strict):
    website_token: str | None = None
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.:-]+$")
    url: str | None = Field(default=None, max_length=2048)
    properties: dict[str, Any] = Field(default_factory=dict, max_length=20)


class UploadRequest(_Strict):
    website_token: str | None = None
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    byte_size: int = Field(gt=0)
