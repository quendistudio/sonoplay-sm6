"""Plan Plex playQueue edits → SM6 playlist SOAP operations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _norm(value: Any) -> str:
    return str(value or "").casefold().strip()


def track_matches_entry(track, entry) -> bool:
    """Match a Plex queue track to an SM6 playlist entry (title + artist)."""
    title = _norm(getattr(track, "title", None))
    artist = _norm(getattr(track, "grandparentTitle", None))
    if not title or title != _norm(entry.title):
        return False
    entry_artist = _norm(entry.artist)
    if artist and entry_artist and artist != entry_artist:
        return False
    return True


@dataclass(frozen=True)
class Sm6DeleteTrack:
    sm6_track_id: int


@dataclass(frozen=True)
class Sm6ClearQueueTail:
    """DeleteAll — clears SM6 upcoming tracks, keeps current playback."""


@dataclass(frozen=True)
class Sm6MoveTrack:
    from_index: int
    to_index: int


@dataclass(frozen=True)
class Sm6InsertTrack:
    insert_position: int
    plex_tail_index: int


Sm6QueueEditOp = Sm6DeleteTrack | Sm6ClearQueueTail | Sm6MoveTrack | Sm6InsertTrack


def sm6_tail_entries(playlist_state, *, media_queue_index: int | None = None) -> list:
    """SM6 playlist entries strictly after the current playback index."""
    if playlist_state is None:
        return []
    index = (
        media_queue_index
        if media_queue_index is not None
        else playlist_state.media_queue_index
    )
    tracks = list(playlist_state.tracks)
    if not tracks or index < 0:
        return []
    return tracks[index + 1 :]


def tail_item_ids_from_tracks(tracks: list, *, selected_offset: int) -> list[int]:
    """playQueueItemIDs for queue rows after the selected track."""
    local_start = max(0, min(selected_offset, len(tracks) - 1))
    tail: list[int] = []
    for track in tracks[local_start + 1 :]:
        item_id = getattr(track, "playQueueItemID", None)
        if item_id is not None:
            tail.append(int(item_id))
    return tail


def plan_deletes_for_removed_items(
    *,
    old_tail_item_ids: list[int],
    new_tail_item_ids: list[int],
    sm6_tail_track_ids: list[int],
) -> list[Sm6DeleteTrack | Sm6ClearQueueTail]:
    """Drop SM6 upcoming tracks when they disappear from the Plex tail.

    Full Plex tail clear → DeleteAll (keeps current playback on SM6).
    Partial removal → DeletePlaylistTrack per removed row.
    """
    if old_tail_item_ids and not new_tail_item_ids:
        return [Sm6ClearQueueTail()]

    new_set = set(new_tail_item_ids)
    item_to_sm6: dict[int, int] = {}
    for index, item_id in enumerate(old_tail_item_ids):
        if index < len(sm6_tail_track_ids):
            item_to_sm6[item_id] = sm6_tail_track_ids[index]

    delete_ops: list[Sm6DeleteTrack] = []
    for item_id in old_tail_item_ids:
        if item_id in new_set:
            continue
        sm6_id = item_to_sm6.get(item_id)
        if sm6_id is not None:
            delete_ops.append(Sm6DeleteTrack(sm6_track_id=sm6_id))
    delete_ops.sort(key=lambda op: op.sm6_track_id, reverse=True)
    return delete_ops


def reconcile_sm6_tail_to_plex(
    *,
    plex_tail_tracks: list,
    sm6_state,
) -> list[Sm6QueueEditOp]:
    """Insert/move/delete on SM6 so its tail matches Plex tail tracks (metadata match)."""
    if sm6_state is None:
        return []

    queue_index = sm6_state.media_queue_index
    sm6_tail = sm6_tail_entries(sm6_state, media_queue_index=queue_index)
    if not plex_tail_tracks and sm6_tail:
        return [Sm6ClearQueueTail()]

    ops: list[Sm6QueueEditOp] = []

    sm6_matched: set[int] = set()
    plex_to_sm6: list[int | None] = []
    for track in plex_tail_tracks:
        matched_index: int | None = None
        for sm6_index, entry in enumerate(sm6_tail):
            if sm6_index in sm6_matched:
                continue
            if track_matches_entry(track, entry):
                matched_index = sm6_index
                sm6_matched.add(sm6_index)
                break
        plex_to_sm6.append(matched_index)

    for sm6_index, entry in enumerate(sm6_tail):
        if sm6_index not in sm6_matched:
            ops.append(Sm6DeleteTrack(sm6_track_id=entry.track_id))

    for plex_index, sm6_index in enumerate(plex_to_sm6):
        if sm6_index is None:
            ops.append(
                Sm6InsertTrack(
                    insert_position=queue_index + 1 + plex_index,
                    plex_tail_index=plex_index,
                )
            )

    order = [sm6_index for sm6_index in plex_to_sm6 if sm6_index is not None]
    physical = sorted(sm6_matched)
    for target, desired_sm6_index in enumerate(order):
        current_pos = physical.index(desired_sm6_index)
        if current_pos == target:
            continue
        from_idx = queue_index + 1 + current_pos
        to_idx = queue_index + 1 + target
        ops.append(Sm6MoveTrack(from_index=from_idx, to_index=to_idx))
        physical.pop(current_pos)
        physical.insert(target, desired_sm6_index)

    return ops


def plan_refresh_inserts(
    new_indices: list[int],
    *,
    selected_after: int,
) -> list[tuple[str, int]]:
    """Map new Plex queue rows to SM6 insert modes ('insert' or 'add' for APPEND)."""
    if not new_indices:
        return []
    indices = sorted(new_indices)
    play_next_start = selected_after + 1
    is_play_next_block = (
        indices[0] == play_next_start
        and indices == list(range(indices[0], indices[0] + len(indices)))
    )
    if not is_play_next_block:
        return [("add", idx) for idx in indices]
    return [
        ("insert", play_next_start + offset)
        for offset in range(len(indices))
    ]
