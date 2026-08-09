"""Resolve Plex ratingKey → Plex DLNA stream URL (Cambridge Stream Magic)."""
from __future__ import annotations

import asyncio
import json
import logging
import xml.sax.saxutils
from pathlib import Path

from settings import atomic_write_json, settings

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
from .dlna_stream_cache import (
    StreamCacheEntry,
    embed_rating_key_in_stream_url,
    normalize_stream_url,
    object_id_from_stream_url,
    patch_didl_res_urls,
    rating_key_from_uri,
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
    from plex.mp3_transcode_cache import cache_file_valid, cache_path_for

    cached = cache_path_for(rating_key, cbr_kbps=cbr_kbps)
    if cache_file_valid(cached):
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
    """Tracks to SKIP_NEXT after loading the album container (1-based Plex track number).

    Prefer ``index`` (track # on disc). ``parentIndex`` is the disc # in Plex JSON;
    keep it only as a legacy fallback for older fixtures that overloaded the name.
    """
    raw = getattr(track, "index", None)
    if raw is None:
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
        self._cache: dict[str, StreamCacheEntry] = {}
        self._object_to_rating: dict[str, str] = {}
        self._didl_cache: dict[str, str] = {}
        self._ids_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()
        if cache_path and cache_path.exists():
            try:
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    streams = raw.get("streams", raw)
                    if isinstance(streams, dict):
                        for key, value in streams.items():
                            entry = StreamCacheEntry.from_raw(value)
                            if entry:
                                self._cache[str(key)] = entry
                                if entry.object_id:
                                    self._object_to_rating[entry.object_id] = str(key)
                    didl = raw.get("didl")
                    if isinstance(didl, dict):
                        self._didl_cache = dict(didl)
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

    async def _store_stream(self, track, item) -> str:
        rating_key = str(getattr(track, "ratingKey", "") or "")
        url = embed_rating_key_in_stream_url(item.url, rating_key)
        oid = object_id_from_stream_url(url)
        entry = StreamCacheEntry.from_track(track, url, object_id=oid)
        async with self._cache_lock:
            self._cache[rating_key] = entry
            if oid:
                self._object_to_rating[oid] = rating_key
            self._persist_cache_unlocked()
        return url

    async def _pick_album_track(self, track, album_title: str, artist: str | None):
        title = getattr(track, "title", None)
        if not title:
            return None
        album_items = await browse_album_tracks(
            self._browser,
            self._musique_id,
            album_title,
            artist=artist,
        )
        target = str(title).casefold()
        matches = [i for i in album_items if i.title.casefold() == target and i.url]
        parent_index = getattr(track, "parentIndex", None)
        duration_ms = getattr(track, "duration", None)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            picked = self._disambiguate_album_tracks(
                track,
                matches,
                album_items,
                parent_index=parent_index,
                duration_ms=duration_ms,
            )
            if picked is not None:
                return picked
            logger.warning(
                "DLNA ambiguous title %r in album %r (%d matches) — using first match",
                title,
                album_title,
                len(matches),
            )
            return matches[0]
        if parent_index is not None:
            try:
                idx = int(parent_index) - 1
                if 0 <= idx < len(album_items) and album_items[idx].url:
                    return album_items[idx]
            except (TypeError, ValueError):
                pass
        return None

    def _disambiguate_album_tracks(
        self,
        track,
        title_matches: list,
        album_items: list,
        *,
        parent_index,
        duration_ms,
    ):
        """Prefer parentIndex, then composite artist/album/title alignment."""
        if parent_index is not None:
            try:
                idx = int(parent_index) - 1
                if 0 <= idx < len(album_items):
                    candidate = album_items[idx]
                    if candidate.url and candidate in title_matches:
                        return candidate
            except (TypeError, ValueError):
                pass
        artist = getattr(track, "grandparentTitle", None) or getattr(track, "parentTitle", None)
        album = getattr(track, "parentTitle", None)
        scored: list[tuple[int, object]] = []
        for item in title_matches:
            score = 0
            if parent_index is not None:
                try:
                    idx = album_items.index(item)
                    if idx == int(parent_index) - 1:
                        score += 8
                except ValueError:
                    pass
            if artist and str(artist).casefold() in item.title.casefold():
                score += 1
            if album and str(album).casefold() in item.title.casefold():
                score += 1
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if not scored:
            return None
        best_score, best_item = scored[0]
        if len(scored) > 1 and scored[1][0] == best_score:
            logger.warning(
                "DLNA disambiguation tie for %r (parentIndex=%s duration=%s) — using %s",
                getattr(track, "title", "?"),
                parent_index,
                duration_ms,
                getattr(best_item, "object_id", "?"),
            )
        return best_item if best_score > 0 or len(scored) == 1 else None

    async def resolve_stream_url(self, track) -> str:
        await self._ensure_dlna_ids()
        rating_key = str(getattr(track, "ratingKey", "") or "")
        if not rating_key:
            raise LookupError("Track without ratingKey")
        async with self._cache_lock:
            cached = self._cache.get(rating_key)
        if cached and cached.matches_track(track):
            logger.info(
                "DLNA URL cache hit ratingKey=%s -> %s",
                rating_key,
                cached.url,
            )
            return cached.url
        if cached and not cached.matches_track(track):
            logger.info(
                "DLNA URL cache invalidated ratingKey=%s (metadata mismatch)",
                rating_key,
            )
            async with self._cache_lock:
                self._cache.pop(rating_key, None)
                if cached.object_id:
                    self._object_to_rating.pop(cached.object_id, None)

        title = getattr(track, "title", None)
        album_title = getattr(track, "parentTitle", None)
        artist = getattr(track, "grandparentTitle", None) or album_title

        if album_title and title:
            item = await self._pick_album_track(
                track,
                str(album_title),
                str(artist) if artist else None,
            )
            if item and item.url:
                return await self._store_stream(track, item)

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
                item = matches.get(str(title).casefold())
                if item and item.url:
                    return await self._store_stream(track, item)

        raise LookupError(f"DLNA URL not found for ratingKey {rating_key} ({title})")

    def rating_key_from_dlna_cache_only(self, url: str | None) -> str | None:
        """Plex ratingKey from DLNA cache / object_id map only (no URI-embedded parse)."""
        if not url:
            return None
        normalized = normalize_stream_url(url)
        for rating_key, entry in self._cache.items():
            if entry.url == url or normalize_stream_url(entry.url) == normalized:
                return rating_key
        object_id = object_id_from_stream_url(url)
        if not object_id:
            return None
        mapped = self._object_to_rating.get(object_id)
        if mapped:
            return mapped
        for rating_key, entry in self._cache.items():
            if entry.object_id == object_id:
                return rating_key
        return None

    def rating_key_for_stream_url(self, url: str | None) -> str | None:
        """Plex ratingKey from TrackURI (embedded sources, then DLNA cache)."""
        if not url:
            return None
        embedded = rating_key_from_uri(url)
        if embedded:
            return embedded
        return self.rating_key_from_dlna_cache_only(url)

    async def tag_didl_rating_keys(self, didl: str, tracks: list) -> str:
        """Tag every ``<res>`` in an album DIDL with Plex ratingKeys."""
        mapping: dict[str, str] = {}
        for track in tracks:
            rating_key = str(getattr(track, "ratingKey", "") or "")
            if not rating_key.isdigit():
                continue
            try:
                stream_url = await self.resolve_stream_url(track)
            except LookupError:
                continue
            object_id = object_id_from_stream_url(stream_url)
            if object_id:
                mapping[object_id] = rating_key
        if not mapping:
            return didl
        return patch_didl_res_urls(didl, mapping)

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
            await self._persist_cache()
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
            return patch_didl_res_urls(
                self._didl_cache[cache_key],
                default_rating_key=rating_key,
            )

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
        didl = patch_didl_res_urls(didl, {dlna_item.object_id: rating_key})
        self._didl_cache[cache_key] = didl
        await self._persist_cache()
        logger.info(
            "Resolved Plex DLNA DIDL for %s (ratingKey=%s, object_id=%s)",
            track_title,
            rating_key,
            dlna_item.object_id,
        )
        return didl

    async def resolve_track_object_id(self, track) -> str | None:
        """Plex DLNA object id for a track (play-from-id / cache)."""
        rating_key = str(getattr(track, "ratingKey", "") or "")
        if rating_key:
            cached = self._cache.get(rating_key)
            if cached and cached.object_id:
                return cached.object_id
        album_title = getattr(track, "parentTitle", None)
        track_title = getattr(track, "title", None)
        if not album_title or not track_title:
            return None
        await self._ensure_dlna_ids()
        artist = getattr(track, "grandparentTitle", None) or album_title
        try:
            dlna_item = await find_track_in_album(
                self._browser,
                self._musique_id,
                str(album_title),
                str(track_title),
                artist=str(artist) if artist else None,
            )
        except LookupError:
            return None
        if rating_key and dlna_item.object_id:
            url = dlna_item.url or ""
            entry = StreamCacheEntry.from_track(
                track,
                url,
                object_id=dlna_item.object_id,
            )
            self._cache[rating_key] = entry
            self._object_to_rating[dlna_item.object_id] = rating_key
            await self._persist_cache()
        return dlna_item.object_id

    async def resolve_play_didl(
        self,
        item,
        *,
        media_kind: str = "track",
        start_playback: bool = True,
        prefer_track_didl: bool = False,
    ) -> tuple[str, str | None, int]:
        """QueueFolder DIDL for SM6 playback (album container when possible).

        Returns ``(didl, play_from_object_id, skip_fallback)``.
        Prefer ``PLAY_FROM_HERE`` + ``play-from-id`` when mid-album; ``skip_fallback``
        is only for the legacy SKIP_NEXT path if the object id is unavailable.

        Cambridge Connect loads ``object.container.album.musicAlbum`` for album
        playback; track-level ``musicTrack`` items use a left-aligned layout on the SM6.
        Multi-track playlists must enqueue one ``musicTrack`` per QueueFolder APPEND.
        """
        if media_kind == "album":
            didl = await self.resolve_didl(item, media_kind="album")
            return didl, None, 0

        track = item
        if prefer_track_didl:
            didl = await self.resolve_didl(track, media_kind="track")
            return didl, None, 0

        album_key = album_rating_key_from_track(track)
        if start_playback and album_key:
            from types import SimpleNamespace

            album_ref = SimpleNamespace(
                ratingKey=str(album_key),
                title=getattr(track, "parentTitle", None),
                parentTitle=getattr(track, "grandparentTitle", None),
            )
            skip = track_skip_count(track)
            logger.info(
                "Resolving Plex DLNA play DIDL via album container "
                "ratingKey=%s album=%r skip=%s",
                getattr(track, "ratingKey", "?"),
                getattr(track, "parentTitle", "?"),
                skip,
            )
            didl = await self.resolve_didl(album_ref, media_kind="album")
            if skip <= 0:
                return didl, None, 0
            play_from_id = await self.resolve_track_object_id(track)
            if play_from_id:
                return didl, play_from_id, 0
            logger.warning(
                "PLAY_FROM_HERE unavailable (no DLNA object_id) — fallback SKIP_NEXT x%s "
                "ratingKey=%s",
                skip,
                getattr(track, "ratingKey", "?"),
            )
            return didl, None, skip

        didl = await self.resolve_didl(track, media_kind="track")
        return didl, None, 0

    def _persist_cache_unlocked(self) -> None:
        if not self._cache_path:
            return
        try:
            streams = {key: entry.to_json() for key, entry in self._cache.items()}
            payload = {"streams": streams, "didl": self._didl_cache}
            atomic_write_json(self._cache_path, payload)
        except OSError as exc:
            logger.warning("Could not persist DLNA URL cache: %s", exc)

    async def _persist_cache(self) -> None:
        async with self._cache_lock:
            self._persist_cache_unlocked()


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
