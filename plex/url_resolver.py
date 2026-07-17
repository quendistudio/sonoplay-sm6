"""Resolve Plex ratingKey → Plex DLNA stream URL (Cambridge Stream Magic)."""
from __future__ import annotations

import asyncio
import json
import logging
import xml.sax.saxutils
from pathlib import Path

from settings import settings

from .dlna_browser import (
    DlnaBrowser,
    browse_album_tracks,
    discover_plex_dlna_music_ids,
    find_album,
    find_items_by_title,
    find_track_in_album,
    load_dlna_discovery_cache,
    save_dlna_discovery_cache,
)

logger = logging.getLogger(__name__)

_DLNA_RESOLVE_TIMEOUT_SECONDS = 60.0
_SM6_ALBUM_PLAY_SKIP_SETTLE_SECONDS = 0.35

_resolver: UrlResolver | None = None


def album_rating_key_from_track(track) -> str | None:
    key = getattr(track, "parentRatingKey", None)
    if key is None:
        return None
    text = str(key).strip()
    return text or None


_MP3_RES_PROTOCOL = (
    "http-get:*:audio/mpeg:"
    "DLNA.ORG_PN=MP3;DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000"
)


def _format_didl_duration(duration_ms: int | None) -> str | None:
    if duration_ms is None or duration_ms <= 0:
        return None
    total_seconds, remainder_ms = divmod(int(duration_ms), 1000)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{remainder_ms:03d}"


def _estimated_mp3_size_bytes(duration_ms: int | None, cbr_kbps: int) -> int | None:
    """CBR MP3 byte size from Plex duration (matches ffmpeg -b:a output closely)."""
    if duration_ms is None or duration_ms <= 0 or cbr_kbps <= 0:
        return None
    return int(duration_ms * cbr_kbps / 8)


def _mp3_size_for_track(rating_key: str, duration_ms: int | None, cbr_kbps: int) -> int | None:
    from plex.mp3_transcode_cache import cache_path_for

    cached = cache_path_for(rating_key, cbr_kbps=cbr_kbps)
    if cached.is_file() and cached.stat().st_size > 0:
        return cached.stat().st_size
    return _estimated_mp3_size_bytes(duration_ms, cbr_kbps)


def build_transcode_track_didl(track, stream_url: str) -> str:
    """Minimal musicTrack DIDL pointing at SonoPlay MP3 proxy (SM6 QueueFolder)."""
    rating_key = str(getattr(track, "ratingKey", "") or "unknown")
    object_id = f"sonoplay-tc-{rating_key}"
    title = xml.sax.saxutils.escape(str(getattr(track, "title", "Unknown") or "Unknown"))
    album = xml.sax.saxutils.escape(str(getattr(track, "parentTitle", "") or ""))
    artist = xml.sax.saxutils.escape(str(getattr(track, "grandparentTitle", "") or ""))
    duration_ms = getattr(track, "duration", None)
    duration_attr = ""
    formatted = _format_didl_duration(duration_ms)
    if formatted:
        duration_attr = f' duration="{formatted}"'
    size_attr = ""
    size_bytes = _mp3_size_for_track(rating_key, duration_ms, settings.audio_transcode_proxy_kbps)
    if size_bytes:
        size_attr = f' size="{size_bytes}"'
    escaped_url = xml.sax.saxutils.escape(stream_url)
    album_xml = f"<upnp:album>{album}</upnp:album>" if album else ""
    artist_xml = f"<upnp:artist>{artist}</upnp:artist>" if artist else ""
    return (
        '<DIDL-Lite xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        f'<item id="{object_id}" parentID="0" restricted="1">'
        f"<dc:title>{title}</dc:title>"
        f"{album_xml}{artist_xml}"
        f'<res{duration_attr}{size_attr} protocolInfo="{_MP3_RES_PROTOCOL}">'
        f"{escaped_url}</res>"
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        "</item></DIDL-Lite>"
    )


def track_skip_count(track) -> int:
    """Tracks to SKIP_NEXT after loading the parent album container (1-based parentIndex)."""
    raw = getattr(track, "parentIndex", None)
    if raw is None:
        return 0
    try:
        index = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, index - 1)


class UrlResolver:
    def __init__(
        self,
        device_url: str,
        *,
        cache_path: Path | None = None,
    ) -> None:
        self._device_url = device_url
        self._browser = DlnaBrowser(device_url)
        self._musique_id: str | None = settings.plex_dlna_musique_id
        self._music_folder_id: str | None = settings.plex_dlna_music_folder_id
        self._cache_path = cache_path
        self._cache: dict[str, str] = {}
        self._didl_cache: dict[str, str] = {}
        self._ids_lock = asyncio.Lock()
        if cache_path and cache_path.exists():
            try:
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._cache = dict(raw.get("streams", raw))
                    self._didl_cache = dict(raw.get("didl", {}))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Could not load DLNA URL cache %s: %s", cache_path, exc)

    async def _ensure_dlna_ids(self) -> None:
        if self._musique_id:
            return
        async with self._ids_lock:
            if self._musique_id:
                return
            cached = load_dlna_discovery_cache(self._device_url)
            if cached and cached.get("musique_id"):
                self._musique_id = cached["musique_id"]
                self._music_folder_id = cached.get("music_folder_id")
                logger.info(
                    "Plex DLNA ids from cache: musique_id=%s music_folder_id=%s",
                    self._musique_id,
                    self._music_folder_id,
                )
                return
            ids = await discover_plex_dlna_music_ids(self._browser)
            musique_id = ids.get("musique_id")
            if not musique_id:
                raise LookupError(
                    "Plex DLNA musique_id not discovered; set PLEX_DLNA_MUSIQUE_ID "
                    "or check PLEX_DLNA_DEVICE_URL"
                )
            self._musique_id = musique_id
            self._music_folder_id = ids.get("music_folder_id")
            save_dlna_discovery_cache(self._device_url, ids)
            logger.info(
                "Plex DLNA ids discovered: musique_id=%s music_folder_id=%s",
                self._musique_id,
                self._music_folder_id,
            )

    async def resolve_stream_url(self, track) -> str:
        await self._ensure_dlna_ids()
        rating_key = str(getattr(track, "ratingKey", "") or "")
        if not rating_key:
            raise LookupError("Track without ratingKey")
        if rating_key in self._cache:
            url = self._cache[rating_key]
            logger.info(
                "DLNA URL cache hit ratingKey=%s -> %s",
                rating_key,
                url,
            )
            return url

        title = getattr(track, "title", None)
        album_title = getattr(track, "parentTitle", None)
        artist = getattr(track, "grandparentTitle", None) or album_title

        if album_title and title:
            for item in await browse_album_tracks(
                self._browser,
                self._musique_id,
                str(album_title),
                artist=str(artist) if artist else None,
            ):
                if item.title.casefold() == str(title).casefold() and item.url:
                    self._cache[rating_key] = item.url
                    self._persist_cache()
                    return item.url

        if title and self._music_folder_id:
            music_root = None
            for item in await self._browser.browse(self._music_folder_id, count=50):
                if item.is_container and item.title.casefold() in {
                    "musique",
                    "music",
                }:
                    music_root = item
                    break
            if music_root is not None:
                matches = await find_items_by_title(
                    self._browser,
                    {str(title)},
                    root_id=music_root.object_id,
                    max_depth=5,
                )
                item = matches.get(str(title))
                if item and item.url:
                    self._cache[rating_key] = item.url
                    self._persist_cache()
                    return item.url

        raise LookupError(f"DLNA URL not found for ratingKey {rating_key} ({title})")

    def rating_key_for_stream_url(self, url: str | None) -> str | None:
        """Plex ratingKey from a Plex DLNA /object/… URL (cache or object id)."""
        if not url:
            return None
        for rating_key, cached_url in self._cache.items():
            if cached_url == url:
                return rating_key
        object_id = _object_id_from_stream_url(url)
        if not object_id:
            return None
        for rating_key, cached_url in self._cache.items():
            if object_id in cached_url:
                return rating_key
        return None

    async def resolve_didl(self, item, *, media_kind: str = "track") -> str:
        """Plex DLNA DIDL for SM6 QueueFolder (album or track)."""
        await self._ensure_dlna_ids()
        if media_kind == "album":
            rating_key = str(getattr(item, "ratingKey", "") or "")
            if not rating_key:
                raise LookupError("Album without ratingKey")
            cache_key = f"album:{rating_key}"
            if cache_key in self._didl_cache:
                return self._didl_cache[cache_key]
            album_title = getattr(item, "title", None)
            if not album_title:
                raise LookupError(f"Album {rating_key} has no title")
            artist = getattr(item, "parentTitle", None)
            logger.info(
                "Resolving Plex DLNA album DIDL ratingKey=%s title=%r artist=%r",
                rating_key,
                album_title,
                artist,
            )
            dlna_item = await find_album(
                self._browser,
                self._musique_id,
                str(album_title),
                artist=str(artist) if artist else None,
            )
            didl = await self._browser.browse_object_didl(dlna_item.object_id)
            self._didl_cache[cache_key] = didl
            self._persist_cache()
            logger.info(
                "Resolved Plex DLNA album DIDL for %s (ratingKey=%s, object_id=%s)",
                album_title,
                rating_key,
                dlna_item.object_id,
            )
            return didl

        track = item
        rating_key = str(getattr(track, "ratingKey", "") or "")
        if not rating_key:
            raise LookupError("Track without ratingKey")
        cache_key = f"track:{rating_key}"
        if cache_key in self._didl_cache:
            return self._didl_cache[cache_key]

        album_title = getattr(track, "parentTitle", None)
        track_title = getattr(track, "title", None)
        if not album_title or not track_title:
            raise LookupError(f"Track {rating_key} incomplete (missing album/title)")
        artist = getattr(track, "grandparentTitle", None) or album_title
        logger.info(
            "Resolving Plex DLNA track DIDL ratingKey=%s title=%r album=%r artist=%r",
            rating_key,
            track_title,
            album_title,
            artist,
        )
        dlna_item = await find_track_in_album(
            self._browser,
            self._musique_id,
            str(album_title),
            str(track_title),
            artist=str(artist) if artist else None,
        )
        didl = await self._browser.browse_object_didl(dlna_item.object_id)
        self._didl_cache[cache_key] = didl
        self._persist_cache()
        logger.info(
            "Resolved Plex DLNA DIDL for %s (ratingKey=%s, object_id=%s)",
            track_title,
            rating_key,
            dlna_item.object_id,
        )
        return didl

    async def resolve_play_didl(
        self,
        item,
        *,
        media_kind: str = "track",
        start_playback: bool = True,
    ) -> tuple[str, int]:
        """QueueFolder DIDL for SM6 playback (album container when possible).

        Cambridge Connect loads ``object.container.album.musicAlbum`` for album
        playback; track-level ``musicTrack`` items use a left-aligned layout on the SM6.
        """
        if media_kind == "album":
            didl = await self.resolve_didl(item, media_kind="album")
            return didl, 0

        track = item
        album_key = album_rating_key_from_track(track)
        if start_playback and album_key:
            from types import SimpleNamespace

            album_ref = SimpleNamespace(
                ratingKey=str(album_key),
                title=getattr(track, "parentTitle", None),
                parentTitle=getattr(track, "grandparentTitle", None),
            )
            logger.info(
                "Resolving Plex DLNA play DIDL via album container "
                "ratingKey=%s album=%r skip=%s",
                getattr(track, "ratingKey", "?"),
                getattr(track, "parentTitle", "?"),
                track_skip_count(track),
            )
            didl = await self.resolve_didl(album_ref, media_kind="album")
            return didl, track_skip_count(track)

        didl = await self.resolve_didl(track, media_kind="track")
        return didl, 0

    def _persist_cache(self) -> None:
        if not self._cache_path:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"streams": self._cache, "didl": self._didl_cache}
            self._cache_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not persist DLNA URL cache: %s", exc)


def _object_id_from_stream_url(url: str | None) -> str | None:
    if not url or "/object/" not in url:
        return None
    return url.split("/object/", 1)[1].split("/", 1)[0]


def get_url_resolver() -> UrlResolver:
    global _resolver
    if _resolver is None:
        device_url = settings.resolved_plex_dlna_device_url()
        if not device_url:
            raise RuntimeError(
                "Plex DLNA not configured: set PLEX_DLNA_DEVICE_URL "
                "or HOST_IP with PLEX_DLNA_PORT"
            )
        _resolver = UrlResolver(
            device_url,
            cache_path=Path(settings.config_path) / "dlna_url_cache.json",
        )
    return _resolver
