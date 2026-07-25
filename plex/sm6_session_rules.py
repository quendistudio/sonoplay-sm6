"""Pure SM6 session rules — behavior contracts testable without adapter mocks."""
from __future__ import annotations

_ACTIVE_TRANSPORT = frozenset({"PLAYING", "PAUSED_PLAYBACK"})
_STOPPED_TRANSPORT = frozenset({"STOPPED", "NO_MEDIA_PRESENT"})


def plex_queue_index_from_sm6(
    *,
    media_queue_index: int,
    current_track_id: int,
    track_ids: tuple[int, ...],
    resolved_queue_map: list[int | None],
    rating_keys_len: int,
) -> int:
    """Map SM6 queue position to Plex playQueue selected index."""
    if 0 <= media_queue_index < len(resolved_queue_map):
        mapped = resolved_queue_map[media_queue_index]
        if mapped is not None and 0 <= mapped < rating_keys_len:
            return mapped
    for index, track_id in enumerate(track_ids):
        if track_id == current_track_id and index < len(resolved_queue_map):
            mapped = resolved_queue_map[index]
            if mapped is not None and 0 <= mapped < rating_keys_len:
                return mapped
    return 0


def plex_playqueue_offset(base_offset: int, sm6_queue_index: int) -> int:
    """Plex playQueue cursor for a SM6 MediaQueueIndex."""
    return int(base_offset) + int(sm6_queue_index)


def accept_sm6_polled_transport_state(
    polled: str,
    current: str | None,
    *,
    outbound_active: bool,
    optimistic_play_active: bool,
) -> str:
    """Effective transport state while SonoPlay owns SM6 playback."""
    if polled == "PLAYING":
        return polled
    if (
        polled in _STOPPED_TRANSPORT
        and current in _ACTIVE_TRANSPORT
        and outbound_active
    ):
        return current
    if (
        polled == "PAUSED_PLAYBACK"
        and current == "PLAYING"
        and optimistic_play_active
    ):
        return current
    return polled


def sm6_stop_should_relinquish(
    previous: str | None,
    polled: str,
    *,
    outbound_active: bool,
) -> bool:
    """True when an SM6 STOP should end the SonoPlay Plex session."""
    return (
        polled in _STOPPED_TRANSPORT
        and previous in _ACTIVE_TRANSPORT
        and not outbound_active
    )


def ignore_stale_plex_volume_write(
    requested: int,
    device_plex: int | None,
    *,
    in_grace_period: bool,
) -> bool:
    """Reject Plex volume writes that disagree with the device during play startup."""
    if not in_grace_period or device_plex is None:
        return False
    device = int(device_plex)
    req = int(requested)
    if req == 0 and device > 5:
        return True
    return abs(req - device) > 20


def sm6_should_detach_on_audio_source(
    previous: int | None,
    current: int | None,
    *,
    media_player_id: int,
    sonoplay_owned: bool,
) -> bool:
    """True when SM6 left Media Player (id 10) while SonoPlay still owned the session.

    First poll on a non-10 source with no ownership does not detach (device already
    on radio/etc. at discovery). Transition away from id 10, or owned playback on
    a non-10 source, does.
    """
    if current is None or current == media_player_id:
        return False
    if previous == current:
        return False
    if previous == media_player_id:
        return True
    return bool(sonoplay_owned)


def sm6_external_playback_allowed(
    last_audio_source: int | None,
    *,
    media_player_id: int,
) -> bool:
    """Front-panel external_playback only while on Media Player source.

    Unknown source (None) still allows the path — audio-source poll may not have
    run yet. Known non-10 means the audio-source detach path owns the reaction.
    """
    if last_audio_source is None:
        return True
    return last_audio_source == media_player_id
