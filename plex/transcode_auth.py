"""Signed SM6 transcode proxy URLs (device UUID + HMAC)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

from settings import settings

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 6 * 3600


def _signing_secret(device_uuid: str) -> str | None:
    """Prefer the paired Plex token for this renderer, then env PMS token."""
    token = settings.get_token_for_uuid(device_uuid)
    if token:
        return str(token)
    if settings.plex_pms_token:
        return str(settings.plex_pms_token)
    return None


def build_signed_transcode_query(
    rating_key: str,
    device_uuid: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    secret = _signing_secret(device_uuid)
    if not secret:
        raise ValueError(
            f"no Plex token to sign SM6 transcode URL for device {device_uuid}"
        )
    key = str(rating_key).strip()
    device = str(device_uuid).strip()
    exp = int(time.time()) + int(ttl_seconds)
    payload = f"{key}|{device}|{exp}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return urlencode({"ratingKey": key, "device": device, "exp": exp, "sig": sig})


def verify_signed_transcode_request(
    rating_key: str,
    device_uuid: str,
    exp: int,
    sig: str,
) -> bool:
    if not rating_key.isdigit():
        return False
    if not device_uuid.strip():
        return False
    if int(exp) < int(time.time()):
        logger.warning("transcode proxy: expired signature device=%s", device_uuid)
        return False
    secret = _signing_secret(device_uuid)
    if not secret:
        return False
    payload = f"{rating_key}|{device_uuid}|{int(exp)}"
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        logger.warning("transcode proxy: bad signature device=%s", device_uuid)
        return False
    return _device_is_registered_sm6(device_uuid)


def _device_is_registered_sm6(device_uuid: str) -> bool:
    from plex.device_profiles import is_sm6_like

    try:
        from dlna.dlna_device import get_device_by_uuid

        device = get_device_by_uuid(device_uuid)
    except Exception as exc:
        logger.debug("transcode auth device lookup failed: %s", exc)
        device = None
    if device is not None and is_sm6_like(device):
        return True
    # Paired SM6 may exist in data.json before SSDP rediscovery.
    return settings.get_token_for_uuid(device_uuid) is not None
