"""Non-expiring HMAC signatures for media URLs.

URLs never expire so embeds in old chats keep loading; the signature only prevents
enumerating the library by guessing ids.
"""

import hashlib
import hmac

from .config import get_settings

VARIANTS = ("full", "thumb", "poster")


def sign(media_id: int, variant: str) -> str:
    key = get_settings().signing_secret.encode()
    msg = f"{media_id}:{variant}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def verify(media_id: int, variant: str, sig: str) -> bool:
    return variant in VARIANTS and hmac.compare_digest(sign(media_id, variant), sig or "")


def media_url(media_id: int, variant: str = "full") -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/m/{media_id}/{variant}?sig={sign(media_id, variant)}"
