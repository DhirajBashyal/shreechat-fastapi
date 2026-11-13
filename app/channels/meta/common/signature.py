"""X-Hub-Signature-256 (CHANNEL_ADAPTER_ARCHITECTURE.md §6).

Always computed over the exact raw bytes: re-serialising parsed JSON changes
whitespace and breaks the signature.
"""

import hashlib
import hmac
from collections.abc import Iterable, Mapping


def sign(secret: bytes, raw_body: bytes) -> str:
    return "sha256=" + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()


def verify_meta_signature(
    headers: Mapping[str, str], raw_body: bytes, secrets: Iterable[bytes]
) -> bool:
    header = ""
    for key, value in headers.items():
        if key.lower() == "x-hub-signature-256":
            header = value
            break
    if not header.startswith("sha256="):
        return False
    received = header.removeprefix("sha256=")
    for secret in secrets:
        if not secret:
            continue
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, received):
            return True
    return False
