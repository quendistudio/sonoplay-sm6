"""SM6 transcode proxy: Plex source part URL → cached CBR MP3 file."""
from __future__ import annotations

import logging

from plex.adapters import PlexLib
from plex.runtime_cache import cached_plex_session
from settings import settings

logger = logging.getLogger(__name__)


def resolve_pms_token_for_proxy() -> str | None:
    if settings.plex_pms_token:
        return settings.plex_pms_token
    for info in (settings.load_data() or {}).values():
        if isinstance(info, dict):
            token = info.get("token")
            if token:
                return str(token)
    return None


def plex_lib_for_transcode_proxy() -> PlexLib | None:
    session = cached_plex_session()
    if not session.get("address"):
        return None
    token = resolve_pms_token_for_proxy()
    if not token:
        logger.warning("transcode proxy: no Plex PMS token available")
        return None
    lib = PlexLib()
    lib.protocol = session.get("protocol") or "http"
    lib.address = session["address"]
    lib.port = int(session["port"])
    lib.machine_id = session.get("machine_id") or None
    lib.token = token
    lib.client_identifier = "sonoplay-mp3-proxy"
    return lib


def sonoplay_base_url() -> str:
    host = settings.host_ip or "127.0.0.1"
    return f"http://{host}:{int(settings.http_port)}"


def build_sm6_transcode_proxy_url(rating_key: str) -> str:
    """URL the SM6 fetches: SonoPlay serves a complete MP3 file."""
    key = str(rating_key).strip()
    if not key.isdigit():
        raise ValueError("ratingKey required for SM6 transcode proxy URL")
    return f"{sonoplay_base_url()}/player/stream/transcode.mp3?ratingKey={key}"


def _part_key_from_track(track) -> str | None:
    media_list = getattr(track, "Media", None)
    if not media_list:
        return None
    media = media_list[0] if isinstance(media_list, list) else media_list
    parts = getattr(media, "Part", None)
    if not parts:
        return None
    part = parts[0] if isinstance(parts, list) else parts
    key = getattr(part, "key", None)
    return str(key) if key else None


async def resolve_pms_source_url_for_rating_key(rating_key: str) -> str | None:
    """Direct Plex part URL (FLAC/file) for ffmpeg input."""
    lib = plex_lib_for_transcode_proxy()
    if lib is None:
        return None
    track = await lib.fetch_metadata(f"/library/metadata/{rating_key}")
    if track is None:
        return None
    part_key = _part_key_from_track(track)
    if not part_key:
        logger.warning("transcode proxy: no part key for ratingKey=%s", rating_key)
        return None
    return lib.build_url(part_key)
