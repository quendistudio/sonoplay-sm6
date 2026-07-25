"""Strict metadata fallback when no Plex ratingKey is available."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TRACK_DURATION_TOLERANCE_MS = 2000


def track_duration_ms(track) -> int | None:
    duration = track.get("duration") if isinstance(track, dict) else getattr(track, "duration", None)
    if duration is None:
        return None
    try:
        value = int(duration)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def sm6_entry_metadata_search_allowed(entry) -> bool:
    """Metadata search only when artist plus album or duration are known."""
    artist = str(getattr(entry, "artist", "") or "").strip()
    if not artist:
        return False
    album = str(getattr(entry, "album", "") or "").strip()
    duration = getattr(entry, "duration_seconds", None)
    if album:
        return True
    if duration is not None:
        try:
            return int(duration) > 0
        except (TypeError, ValueError):
            return False
    return False


def disambiguate_tracks_by_duration(
    candidates: list,
    *,
    duration_seconds: int | None,
    tolerance_ms: int = TRACK_DURATION_TOLERANCE_MS,
) -> list:
    """Keep candidates whose Plex duration matches SM6 entry duration."""
    if duration_seconds is None or len(candidates) <= 1:
        return candidates
    try:
        target_ms = int(duration_seconds) * 1000
    except (TypeError, ValueError):
        return candidates
    matched = []
    for candidate in candidates:
        duration_ms = track_duration_ms(candidate)
        if duration_ms is None:
            continue
        if abs(duration_ms - target_ms) <= tolerance_ms:
            matched.append(candidate)
    return matched if matched else candidates


def pick_unique_track(candidates: list, *, label: str) -> object | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    keys = []
    for item in candidates:
        if isinstance(item, dict):
            keys.append(str(item.get("ratingKey", "") or ""))
        else:
            keys.append(str(getattr(item, "ratingKey", "") or ""))
    keys = sorted({key for key in keys if key})
    logger.warning(
        "metadata search ambiguous for %r (%d matches, ratingKeys=%s)",
        label,
        len(candidates),
        keys[:6],
    )
    return None
