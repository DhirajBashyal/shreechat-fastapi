"""Graph API error → adapter error (WHATSAPP.md §5, FACEBOOK_MESSENGER.md §5,
INSTAGRAM.md §5)."""

from typing import Any

from app.channels.base import (
    ChannelError,
    ConnectionBrokenError,
    PermanentChannelError,
    ReauthorizationRequiredError,
    RetryableChannelError,
    WindowClosedError,
)

AUTH_CODES = {190, 10, 200}
TEMPORARY_CODES = {1, 2, 1200}

WHATSAPP: dict[int, tuple[type[ChannelError], str, float | None]] = {
    131047: (WindowClosedError, "window_closed", None),
    131026: (PermanentChannelError, "undeliverable", None),
    131051: (PermanentChannelError, "unsupported_message_type", None),
    131052: (RetryableChannelError, "media_download_error", None),
    131053: (RetryableChannelError, "media_upload_error", None),
    131056: (RetryableChannelError, "pair_rate_limit", 60.0),
    130429: (RetryableChannelError, "rate_limited", None),
    131048: (RetryableChannelError, "spam_rate_limit", None),
    80007: (RetryableChannelError, "rate_limited", None),
    131031: (ConnectionBrokenError, "account_locked", None),
    368: (ConnectionBrokenError, "policy_blocked", None),
    131000: (RetryableChannelError, "temporary_error", None),
    131016: (RetryableChannelError, "service_unavailable", None),
}

MESSAGING: dict[int, tuple[type[ChannelError], str, float | None]] = {
    551: (PermanentChannelError, "recipient_unavailable", None),
    1545041: (PermanentChannelError, "recipient_unavailable", None),
    4: (RetryableChannelError, "rate_limited", None),
    32: (RetryableChannelError, "rate_limited", None),
    613: (RetryableChannelError, "rate_limited", None),
    2534022: (WindowClosedError, "window_closed", None),  # Instagram
    2534014: (PermanentChannelError, "recipient_unavailable", None),  # Instagram
}


def map_graph_error(
    product: str,
    http_status: int,
    body: Any,
    retry_after: str | None = None,
) -> ChannelError:
    err = body.get("error", {}) if isinstance(body, dict) else {}
    try:
        raw_code = err.get("code")
        code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        code = None
    subcode = err.get("error_subcode")
    message = str(err.get("message") or f"HTTP {http_status}")[:500]
    title = (
        err.get("error_data", {}).get("details")
        if isinstance(err.get("error_data"), dict)
        else None
    )
    detail = f"{code}: {title or message}" if code is not None else message
    delay = float(retry_after) if retry_after and retry_after.isdigit() else None

    if product == "whatsapp_cloud" and code is not None:
        if 132000 <= code <= 132016:
            return PermanentChannelError(detail, code="template_error")
        if code in WHATSAPP:
            cls, name, min_delay = WHATSAPP[code]
            return cls(detail, code=name, retry_after=delay or min_delay)
    if product != "whatsapp_cloud":
        if code == 10 and subcode == 2018278:
            return WindowClosedError(detail)
        if code in MESSAGING:
            cls, name, min_delay = MESSAGING[code]
            return cls(detail, code=name, retry_after=delay or min_delay)
        if subcode in MESSAGING:
            cls, name, min_delay = MESSAGING[subcode]
            return cls(detail, code=name, retry_after=delay or min_delay)
    if code in AUTH_CODES:
        return ReauthorizationRequiredError(detail)
    if code in TEMPORARY_CODES or http_status >= 500 or http_status == 429:
        return RetryableChannelError(detail, retry_after=delay)
    return PermanentChannelError(detail, code="provider_error")
