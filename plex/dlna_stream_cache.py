"""DLNA stream URL cache entries (ratingKey → URL + metadata)."""
from __future__ import annotations

import re
import xml.sax.saxutils
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

_RATING_KEY_PATH = re.compile(r"/library/metadata/(\d+)(?:/|$|\?)")
_RES_TAG = re.compile(r"(<res[^>]*>)(.*?)(</res>)", re.S)


@dataclass
class StreamCacheEntry:
    url: str
    object_id: str | None = None
    title: str | None = None
    album: str | None = None
    artist: str | None = None
    parent_index: int | None = None
    duration_ms: int | None = None

    def matches_track(self, track) -> bool:
        title = getattr(track, "title", None)
        album = getattr(track, "parentTitle", None)
        artist = getattr(track, "grandparentTitle", None) or album
        if title and self.title and str(title).casefold() != str(self.title).casefold():
            return False
        if album and self.album and str(album).casefold() != str(self.album).casefold():
            return False
        if artist and self.artist and str(artist).casefold() != str(self.artist).casefold():
            return False
        parent_index = getattr(track, "parentIndex", None)
        if (
            self.parent_index is not None
            and parent_index is not None
            and int(self.parent_index) != int(parent_index)
        ):
            return False
        duration_ms = getattr(track, "duration", None)
        if (
            self.duration_ms is not None
            and duration_ms is not None
            and abs(int(self.duration_ms) - int(duration_ms)) > 2000
        ):
            return False
        return True

    @classmethod
    def from_track(cls, track, url: str, *, object_id: str | None = None) -> StreamCacheEntry:
        parent_index = getattr(track, "parentIndex", None)
        duration_ms = getattr(track, "duration", None)
        return cls(
            url=url,
            object_id=object_id,
            title=getattr(track, "title", None),
            album=getattr(track, "parentTitle", None),
            artist=getattr(track, "grandparentTitle", None) or getattr(track, "parentTitle", None),
            parent_index=int(parent_index) if parent_index is not None else None,
            duration_ms=int(duration_ms) if duration_ms is not None else None,
        )

    @classmethod
    def from_raw(cls, raw: Any) -> StreamCacheEntry | None:
        if isinstance(raw, str):
            return cls(url=raw)
        if isinstance(raw, dict) and raw.get("url"):
            parent_index = raw.get("parent_index")
            duration_ms = raw.get("duration_ms")
            return cls(
                url=str(raw["url"]),
                object_id=raw.get("object_id"),
                title=raw.get("title"),
                album=raw.get("album"),
                artist=raw.get("artist"),
                parent_index=int(parent_index) if parent_index is not None else None,
                duration_ms=int(duration_ms) if duration_ms is not None else None,
            )
        return None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def object_id_from_stream_url(url: str | None) -> str | None:
    if not url or "/object/" not in url:
        return None
    return url.split("/object/", 1)[1].split("/", 1)[0]


def normalize_stream_url(url: str | None) -> str:
    """Strip sonoplay ``ratingKey`` query param so tagged and raw URLs match."""
    if not url:
        return ""
    parsed = urlparse(str(url).strip())
    query = parse_qs(parsed.query, keep_blank_values=True)
    filtered = {
        name: values for name, values in query.items() if name.casefold() != "ratingkey"
    }
    return urlunparse(parsed._replace(query=urlencode(filtered, doseq=True), fragment=""))


def embed_rating_key_in_stream_url(url: str | None, rating_key: str | None) -> str:
    """Append ``?ratingKey=`` to Plex DLNA stream URLs when absent (SM6 TrackURI tagging)."""
    if not url or not rating_key:
        return str(url or "")
    text = str(url).strip()
    key = str(rating_key).strip()
    if not text or not key.isdigit() or rating_key_from_query_param(text):
        return text
    parsed = urlparse(text)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["ratingKey"] = [key]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True), fragment=""))


def patch_didl_res_urls(
    didl: str,
    object_to_rating_key: dict[str, str] | None = None,
    *,
    default_rating_key: str | None = None,
) -> str:
    """Embed ``ratingKey`` in every DIDL ``<res>`` URL (track or album container)."""
    mapping = object_to_rating_key or {}

    def rating_key_for_url(stream_url: str) -> str | None:
        object_id = object_id_from_stream_url(stream_url)
        if object_id and object_id in mapping:
            return mapping[object_id]
        return default_rating_key

    def replace_res(match: re.Match[str]) -> str:
        open_tag, raw_url, close_tag = match.groups()
        stream_url = raw_url.strip()
        rating_key = rating_key_for_url(stream_url)
        if not rating_key:
            return match.group(0)
        tagged = embed_rating_key_in_stream_url(stream_url, rating_key)
        if tagged == stream_url:
            return match.group(0)
        return f"{open_tag}{xml.sax.saxutils.escape(tagged)}{close_tag}"

    return _RES_TAG.sub(replace_res, didl)


def rating_key_from_query_param(url: str | None) -> str | None:
    """``?ratingKey=`` query param (SonoPlay, SM6, or any third-party tagger)."""
    if not url:
        return None
    parsed = urlparse(str(url).strip())
    for name, values in parse_qs(parsed.query).items():
        if name.casefold() == "ratingkey" and values:
            candidate = str(values[0]).strip()
            if candidate.isdigit():
                return candidate
    return None


def rating_key_from_pms_uri(url: str | None) -> str | None:
    """PMS metadata path: ``server://…/library/metadata/{id}`` or ``/library/metadata/{id}``."""
    if not url:
        return None
    match = _RATING_KEY_PATH.search(str(url).strip())
    if match:
        return match.group(1)
    return None


def rating_key_from_sonoplay_transcode_object(url: str | None) -> str | None:
    """SonoPlay transcode proxy object id ``sonoplay-tc-{ratingKey}``."""
    if not url:
        return None
    sonoplay = re.search(r"sonoplay-tc-(\d+)", str(url).strip(), re.I)
    if sonoplay:
        return sonoplay.group(1)
    return None


def rating_key_from_uri(url: str | None) -> str | None:
    """Extract Plex ratingKey embedded in SM6 TrackURI when present."""
    return (
        rating_key_from_query_param(url)
        or rating_key_from_pms_uri(url)
        or rating_key_from_sonoplay_transcode_object(url)
    )


def is_plex_dlna_stream_uri(url: str | None) -> bool:
    """True when URI looks like a Plex DLNA stream or SonoPlay transcode proxy."""
    if not url:
        return False
    text = str(url).strip()
    if not text:
        return False
    if rating_key_from_uri(text):
        return True
    if object_id_from_stream_url(text):
        return True
    lower = text.casefold()
    if "transcode.mp3" in lower:
        return True
    if "/object/" in lower and (":32469/" in lower or "plex" in lower):
        return True
    return False
