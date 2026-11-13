"""Channel setup schemas (docs/backend/API_DESIGN.md §7–11)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderInfo(BaseModel):
    provider: str
    setup: str  # builtin | manual | oauth | embedded_signup
    configured: bool  # the platform's Meta app settings are present
    release: str  # mvp | r1


class ConnectionRead(BaseModel):
    status: str
    last_error: str | None
    token_expires_at: datetime | None


class InboxSummary(BaseModel):
    id: uuid.UUID
    name: str
    provider: str
    connection: ConnectionRead


class OAuthStart(_Strict):
    inbox_id: uuid.UUID | None = None  # set = reauthorize this inbox


class OAuthStartResponse(BaseModel):
    authorize_url: str


class PageOption(BaseModel):
    page_id: str
    page_name: str | None
    instagram_id: str | None
    can_message: bool  # the user has the MESSAGING task on the Page
    connected_inbox_id: uuid.UUID | None  # already connected in this organization


class MessengerConnect(_Strict):
    selection_token: str
    page_id: str
    inbox_name: str = Field(min_length=1, max_length=255)


class MessengerReauthorize(_Strict):
    selection_token: str
    inbox_id: uuid.UUID


class MessengerConnected(BaseModel):
    inbox: InboxSummary
    page: dict[str, Any]


class InstagramSelection(BaseModel):
    instagram_user_id: str
    username: str | None
    name: str | None


class InstagramConnect(_Strict):
    selection_token: str
    inbox_name: str = Field(min_length=1, max_length=255)


class InstagramReauthorize(_Strict):
    selection_token: str
    inbox_id: uuid.UUID


class InstagramConnected(BaseModel):
    inbox: InboxSummary
    account: dict[str, Any]


class WhatsAppManual(_Strict):
    inbox_name: str = Field(min_length=1, max_length=255)
    waba_id: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")
    phone_number_id: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")
    access_token: str = Field(min_length=10, max_length=4096)
    inbox_id: uuid.UUID | None = None  # reauthorize an existing inbox


class WhatsAppEmbeddedSignup(_Strict):
    inbox_name: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=4096)
    waba_id: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")
    phone_number_id: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")
    inbox_id: uuid.UUID | None = None


class WhatsAppConnected(BaseModel):
    inbox: InboxSummary
    phone_number: dict[str, Any]
    templates_synced: int


class TemplateRead(BaseModel):
    id: uuid.UUID
    name: str
    language: str
    category: str
    status: str
    parameter_format: str
    components: list[dict[str, Any]]
    synced_at: datetime


class HealthResponse(BaseModel):
    ok: bool
    status: str
    details: dict[str, Any]
    error: str | None


class BusinessProfile(BaseModel):
    about: str | None = None
    address: str | None = None
    description: str | None = None
    email: str | None = None
    websites: list[str] | None = None
    vertical: str | None = None


class BusinessProfileUpdate(_Strict):
    about: str | None = Field(default=None, max_length=139)
    address: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=512)
    email: str | None = Field(default=None, max_length=128)
    websites: list[str] | None = Field(default=None, max_length=2)
    vertical: str | None = None
