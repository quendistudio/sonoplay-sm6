"""Persistently skip incompatible DLNA devices discovered via SSDP."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from settings import settings

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_loaded = False
_locations: dict[str, dict[str, Any]] = {}

_PERMANENT_REJECT = re.compile(r"^not valid dlna device\b", re.I)


def cache_path() -> Path:
    return Path(settings.config_path) / "dlna_reject_cache.json"


def normalize_location_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    parsed = urlparse(url)
    path = parsed.path or "/"
    return urlunparse(
        (
            (parsed.scheme or "http").lower(),
            parsed.netloc.lower(),
            path,
            parsed.params,
            parsed.query,
            "",
        )
    )


def _ensure_loaded() -> None:
    global _loaded, _locations
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        path = cache_path()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    entries = raw.get("locations")
                    if isinstance(entries, dict):
                        _locations = {
                            normalize_location_url(key): value
                            for key, value in entries.items()
                            if isinstance(value, dict)
                        }
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Could not load DLNA reject cache %s: %s", path, exc)
        _loaded = True


def _persist() -> None:
    path = cache_path()
    payload = {"locations": _locations}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not persist DLNA reject cache: %s", exc)


def is_rejected(location_url: str) -> bool:
    _ensure_loaded()
    key = normalize_location_url(location_url)
    if not key:
        return False
    with _lock:
        return key in _locations


def is_permanent_reject(exc: BaseException) -> bool:
    return bool(_PERMANENT_REJECT.search(str(exc)))


def remember_rejection(
    location_url: str,
    *,
    reason: str | None = None,
    name: str | None = None,
) -> None:
    _ensure_loaded()
    key = normalize_location_url(location_url)
    if not key:
        return
    if reason and not name:
        match = _PERMANENT_REJECT.search(reason)
        if match:
            tail = reason[match.end() :].strip()
            if tail:
                name = tail.split()[0] if tail else None
    entry = {
        "reason": reason or "incompatible",
        "rejected_at": time.time(),
    }
    if name:
        entry["name"] = name
    with _lock:
        if key in _locations:
            return
        _locations[key] = entry
        _persist()
    label = name or key
    logger.info(
        "DLNA reject cache: %s (%s)",
        label,
        entry["reason"],
    )


def forget_location(location_url: str) -> bool:
    """Remove one cached rejection (manual recovery)."""
    _ensure_loaded()
    key = normalize_location_url(location_url)
    with _lock:
        if key not in _locations:
            return False
        del _locations[key]
        _persist()
        return True
