"""Persist Plex connection hints discovered at runtime (SSDP, client sessions, API)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from settings import settings

logger = logging.getLogger(__name__)

_PLEX_DLNA_PORT = 32469


def cache_path() -> Path:
    return Path(settings.config_path) / "plex_runtime_cache.json"


def load_runtime_cache() -> dict[str, Any]:
    path = cache_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load Plex runtime cache %s: %s", path, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def merge_runtime_cache(**updates: Any) -> None:
    data = load_runtime_cache()
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if data.get(key) != value:
            data[key] = value
            changed = True
    if not changed:
        return
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not persist Plex runtime cache: %s", exc)


def is_probable_plex_dlna_url(url: str) -> bool:
    """Heuristic: Plex Media Server DLNA advertises on port 32469."""
    parsed = urlparse(url)
    return (
        parsed.path.rstrip("/").endswith("DeviceDescription.xml")
        and parsed.port == _PLEX_DLNA_PORT
    )


def remember_plex_dlna_device_url(url: str) -> None:
    merge_runtime_cache(plex_dlna_device_url=url)
    logger.info("Plex DLNA device URL cached: %s", url)


def cached_plex_dlna_device_url() -> str | None:
    url = load_runtime_cache().get("plex_dlna_device_url")
    return str(url) if url else None


def remember_plex_session(
    *,
    protocol: str,
    address: str,
    port: int,
    machine_id: str | None = None,
) -> None:
    merge_runtime_cache(
        pms_protocol=protocol or "http",
        pms_address=address,
        pms_port=int(port),
        pms_machine_id=machine_id or "",
    )


def cached_plex_session() -> dict[str, Any]:
    data = load_runtime_cache()
    if not data.get("pms_address"):
        return {}
    return {
        "protocol": data.get("pms_protocol") or "http",
        "address": data.get("pms_address"),
        "port": int(data.get("pms_port") or settings.plex_pms_port),
        "machine_id": data.get("pms_machine_id") or "",
    }


def remember_music_library_key(key: str) -> None:
    merge_runtime_cache(plex_music_library_key=str(key))


def cached_music_library_key() -> str | None:
    key = load_runtime_cache().get("plex_music_library_key")
    return str(key) if key else None


def remember_plex_dlna_server_udn(udn: str) -> None:
    merge_runtime_cache(plex_dlna_server_udn=str(udn).strip())


def cached_plex_dlna_server_udn() -> str | None:
    udn = load_runtime_cache().get("plex_dlna_server_udn")
    return str(udn).strip() if udn else None
