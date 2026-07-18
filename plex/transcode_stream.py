"""SM6 transcode proxy: Plex source part URL → cached CBR MP3 file."""
from __future__ import annotations

import logging
import os

from plex.adapters import PlexLib
from plex.runtime_cache import cached_plex_session
from plex.transcode_auth import build_signed_transcode_query
from settings import settings

logger = logging.getLogger(__name__)


def resolve_pms_token_for_proxy(*, device_uuid: str | None = None) -> str | None:
    """Deterministic PMS token: env → device pairing → sole registered device."""
    if settings.plex_pms_token:
        return str(settings.plex_pms_token)
    if device_uuid:
        token = settings.get_token_for_uuid(device_uuid)
        if token:
            return str(token)
    data = settings.load_data() or {}
    tokens: list[str] = []
    for uuid, info in data.items():
        if not isinstance(info, dict):
            continue
        token = info.get("token")
        if token:
            tokens.append(str(token))
    unique = list(dict.fromkeys(tokens))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        logger.error(
            "Multiple Plex accounts paired (%s tokens). Set PLEX_PMS_TOKEN explicitly "
            "or pair the target renderer (device=%s).",
            len(unique),
            device_uuid or "?",
        )
        return None
    return None


def plex_lib_for_transcode_proxy(*, device_uuid: str | None = None) -> PlexLib | None:
    session = cached_plex_session()
    if not session.get("address"):
        return None
    token = resolve_pms_token_for_proxy(device_uuid=device_uuid)
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


def ensure_sonoplay_host_ip() -> str:
    """HOST_IP must be reachable by the SM6 (not loopback)."""
    host = (settings.host_ip or "").strip()
    if host and host not in ("127.0.0.1", "0.0.0.0"):
        return host
    env_host = (os.environ.get("HOST_IP") or "").strip()
    if env_host and env_host not in ("127.0.0.1", "0.0.0.0"):
        settings.host_ip = env_host
        return env_host
    raise RuntimeError(
        "HOST_IP must be set to this machine's LAN address for SM6 transcode proxy URLs"
    )


def sonoplay_base_url() -> str:
    host = ensure_sonoplay_host_ip()
    return f"http://{host}:{int(settings.http_port)}"


def build_sm6_transcode_proxy_url(rating_key: str, device_uuid: str) -> str:
    """URL the SM6 fetches: signed SonoPlay MP3 stream."""
    key = str(rating_key).strip()
    if not key.isdigit():
        raise ValueError("ratingKey required for SM6 transcode proxy URL")
    query = build_signed_transcode_query(key, device_uuid)
    return f"{sonoplay_base_url()}/player/stream/transcode.mp3?{query}"


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


async def resolve_pms_source_for_rating_key(
    rating_key: str,
    *,
    device_uuid: str | None = None,
) -> tuple[str, str] | None:
    """Plex part URL without token + token for ffmpeg -headers."""
    lib = plex_lib_for_transcode_proxy(device_uuid=device_uuid)
    if lib is None:
        return None
    track = await lib.fetch_metadata(f"/library/metadata/{rating_key}")
    if track is None:
        return None
    part_key = _part_key_from_track(track)
    if not part_key:
        logger.warning("transcode proxy: no part key for ratingKey=%s", rating_key)
        return None
    return lib.build_url(part_key, token=False), str(lib.token)
