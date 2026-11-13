"""Meta ``signed_request`` (deauthorize and data-deletion callbacks).

signed_request = base64url(HMAC-SHA256(payload, app_secret)) "." base64url(payload)
"""

import base64
import hashlib
import hmac
import json
from typing import Any


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def parse_signed_request(
    signed_request: str, secrets: list[bytes]
) -> dict[str, Any] | None:
    """The payload if the signature matches one of the secrets, else None."""
    try:
        encoded_sig, payload = signed_request.split(".", 1)
        signature = _b64decode(encoded_sig)
        data = json.loads(_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(data, dict)
        or str(data.get("algorithm", "")).upper() != "HMAC-SHA256"
    ):
        return None
    for secret in secrets:
        if secret and hmac.compare_digest(
            hmac.new(secret, payload.encode(), hashlib.sha256).digest(), signature
        ):
            return data
    return None


def make_signed_request(data: dict[str, Any], secret: bytes) -> str:
    """For tests and local tooling."""
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=") + "." + payload
