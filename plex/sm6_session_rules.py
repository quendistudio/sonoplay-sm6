"""Pure SM6 session rules — behavior contracts testable without adapter mocks."""
from __future__ import annotations

_ACTIVE_TRANSPORT = frozenset({"PLAYING", "PAUSED_PLAYBACK"})
_STOPPED_TRANSPORT = frozenset({"STOPPED", "NO_MEDIA_PRESENT"})
# Local states that mean "we still intend to play" — SM6 STOPPED flickers must not win.
_INTENDED_PLAY = _ACTIVE_TRANSPORT | {"TRANSITIONING"}


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
    sonoplay_owned: bool = False,
) -> str:
    """Effective transport state: a SM6 STOPPED flicker must not become the Plex timeline.

    While we still intend to play (owned session or we just sent a command), keep
    PLAYING / PAUSED / TRANSITIONING. A real front-panel stop clears ownership
    first (sm6_stop_should_relinquish), then STOPPED is accepted.
    """
    if polled == "PLAYING":
        return polled
    if (
        polled in _STOPPED_TRANSPORT
        and current in _INTENDED_PLAY
        and (outbound_active or sonoplay_owned)
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


_POWER_OFF_LIKE = frozenset({"OFF", "IDLE"})
_POWER_ON = "ON"


def sm6_plex_stop_should_clear_playlist() -> bool:
    """Plex STOP clears the client play queue — SM6 must DeleteAll to match.

    A single Plex STOP is enough (Plex UI does not offer a useful second STOP).
    """
    return True


def sm6_power_transition_should_clear_playlist(
    previous: str | None,
    current: str | None,
) -> bool:
    """True when a power transition should clear the SM6 playlist (like Plex STOP).

    ON → OFF/IDLE, or OFF/IDLE → ON (missed sleep). Unchanged / unknown / first
    poll while already ON does not clear.
    """
    if current is None or previous is None:
        return False
    if previous == current:
        return False
    prev_off = previous in _POWER_OFF_LIKE
    curr_off = current in _POWER_OFF_LIKE
    prev_on = previous == _POWER_ON
    curr_on = current == _POWER_ON
    if prev_on and curr_off:
        return True
    if prev_off and curr_on:
        return True
    return False


def sm6_connect_should_rebuild_from_playlist(
    power: str | None,
    *,
    playlist_length: int,
) -> bool:
    """True when connect should import SM6 playlist into a Plex playQueue."""
    if power != _POWER_ON:
        return False
    return int(playlist_length) > 0
