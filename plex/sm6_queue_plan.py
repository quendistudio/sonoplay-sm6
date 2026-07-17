"""Plan SM6 QueueFolder loads: album containers vs individual tracks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Sm6QueueSegment:
    kind: Literal["album", "track"]
    parent_rating_key: str | None
    tracks: tuple[Any, ...]


def _parent_key(track: Any) -> str | None:
    key = getattr(track, "parentRatingKey", None)
    if key is None:
        return None
    text = str(key).strip()
    return text or None


def _parent_index(track: Any) -> int | None:
    raw = getattr(track, "parentIndex", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_full_album_run(run: list[Any], album_track_count: int) -> bool:
    """True when *run* is the complete album in disc order (indices 1..N)."""
    if album_track_count <= 0 or len(run) != album_track_count:
        return False
    indices = [_parent_index(track) for track in run]
    if any(index is None for index in indices):
        return False
    return indices == list(range(1, album_track_count + 1))


def plan_queue_segments(
    tracks: list[Any],
    album_track_counts: dict[str, int],
    *,
    is_track_playable=None,
) -> list[Sm6QueueSegment]:
    """Group queue tracks into SM6 QueueFolder segments (1 SOAP call each)."""
    if not tracks:
        return []

    segments: list[Sm6QueueSegment] = []
    index = 0
    while index < len(tracks):
        parent = _parent_key(tracks[index])
        if not parent:
            segments.append(Sm6QueueSegment("track", None, (tracks[index],)))
            index += 1
            continue

        end = index + 1
        while end < len(tracks) and _parent_key(tracks[end]) == parent:
            end += 1
        run = tracks[index:end]
        album_count = album_track_counts.get(parent, 0)
        album_eligible = is_full_album_run(run, album_count)
        if album_eligible and is_track_playable is not None:
            album_eligible = all(is_track_playable(track) for track in run)
        if album_eligible:
            segments.append(Sm6QueueSegment("album", parent, tuple(run)))
        else:
            for track in run:
                segments.append(
                    Sm6QueueSegment("track", _parent_key(track), (track,)),
                )
        index = end
    return segments
