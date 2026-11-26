"""Behavior tests for SM6 session rules (no adapter private API)."""

from plex.sm6_session_rules import (
    accept_sm6_polled_transport_state,
    ignore_stale_plex_volume_write,
    plex_playqueue_offset,
    plex_queue_index_from_sm6,
    sm6_connect_should_rebuild_from_playlist,
    sm6_external_playback_allowed,
    sm6_plex_stop_should_clear_playlist,
    sm6_power_transition_should_clear_playlist,
    sm6_should_detach_on_audio_source,
    sm6_stop_should_relinquish,
)


def test_plex_playqueue_offset_adds_base() -> None:
    assert plex_playqueue_offset(10, 3) == 13


def test_plex_queue_index_prefers_media_queue_index() -> None:
    index = plex_queue_index_from_sm6(
        media_queue_index=4,
        current_track_id=2,
        track_ids=(0, 1, 2, 3, 4),
        resolved_queue_map=[0, 1, 2, 3, 4],
        rating_keys_len=5,
    )
    assert index == 4


def test_plex_queue_index_falls_back_to_current_track_id() -> None:
    index = plex_queue_index_from_sm6(
        media_queue_index=-1,
        current_track_id=1,
        track_ids=(0, 1, 2),
        resolved_queue_map=[0, 1, 2],
        rating_keys_len=3,
    )
    assert index == 1


def test_transport_keeps_playing_during_outbound_stop_glitch() -> None:
    accepted = accept_sm6_polled_transport_state(
        "STOPPED",
        "PLAYING",
        outbound_active=True,
        optimistic_play_active=False,
    )
    assert accepted == "PLAYING"


def test_transport_keeps_playing_while_owned_without_outbound() -> None:
    accepted = accept_sm6_polled_transport_state(
        "STOPPED",
        "PLAYING",
        outbound_active=False,
        optimistic_play_active=False,
        sonoplay_owned=True,
    )
    assert accepted == "PLAYING"


def test_transport_keeps_transitioning_while_owned() -> None:
    accepted = accept_sm6_polled_transport_state(
        "STOPPED",
        "TRANSITIONING",
        outbound_active=False,
        optimistic_play_active=False,
        sonoplay_owned=True,
    )
    assert accepted == "TRANSITIONING"


def test_transport_accepts_stop_when_not_owned() -> None:
    accepted = accept_sm6_polled_transport_state(
        "STOPPED",
        "PLAYING",
        outbound_active=False,
        optimistic_play_active=False,
        sonoplay_owned=False,
    )
    assert accepted == "STOPPED"


def test_transport_accepts_device_pause_after_optimistic_window() -> None:
    accepted = accept_sm6_polled_transport_state(
        "PAUSED_PLAYBACK",
        "PLAYING",
        outbound_active=True,
        optimistic_play_active=False,
    )
    assert accepted == "PAUSED_PLAYBACK"


def test_transport_masks_pause_while_optimistic_play_active() -> None:
    accepted = accept_sm6_polled_transport_state(
        "PAUSED_PLAYBACK",
        "PLAYING",
        outbound_active=False,
        optimistic_play_active=True,
    )
    assert accepted == "PLAYING"


def test_device_stop_relinquishes_when_not_outbound() -> None:
    assert sm6_stop_should_relinquish(
        "PLAYING",
        "STOPPED",
        outbound_active=False,
    )


def test_device_stop_ignored_during_outbound() -> None:
    assert not sm6_stop_should_relinquish(
        "PLAYING",
        "STOPPED",
        outbound_active=True,
    )


def test_volume_grace_ignores_stale_zero() -> None:
    assert ignore_stale_plex_volume_write(
        0,
        85,
        in_grace_period=True,
    )


def test_volume_grace_ignores_large_delta() -> None:
    assert ignore_stale_plex_volume_write(
        10,
        85,
        in_grace_period=True,
    )


def test_volume_grace_allows_close_match() -> None:
    assert not ignore_stale_plex_volume_write(
        80,
        85,
        in_grace_period=True,
    )


def test_volume_grace_inactive_after_window() -> None:
    assert not ignore_stale_plex_volume_write(
        0,
        85,
        in_grace_period=False,
    )


def test_detach_when_leaving_media_player_source() -> None:
    assert sm6_should_detach_on_audio_source(
        10,
        3,
        media_player_id=10,
        sonoplay_owned=False,
    )


def test_no_detach_on_first_poll_non_media_player() -> None:
    assert not sm6_should_detach_on_audio_source(
        None,
        3,
        media_player_id=10,
        sonoplay_owned=False,
    )


def test_detach_when_owned_on_non_media_player() -> None:
    assert sm6_should_detach_on_audio_source(
        None,
        3,
        media_player_id=10,
        sonoplay_owned=True,
    )


def test_no_detach_when_source_unchanged() -> None:
    assert not sm6_should_detach_on_audio_source(
        3,
        3,
        media_player_id=10,
        sonoplay_owned=True,
    )


def test_no_detach_while_on_media_player() -> None:
    assert not sm6_should_detach_on_audio_source(
        3,
        10,
        media_player_id=10,
        sonoplay_owned=True,
    )


def test_external_playback_blocked_off_media_player() -> None:
    assert not sm6_external_playback_allowed(3, media_player_id=10)


def test_external_playback_allowed_on_media_player() -> None:
    assert sm6_external_playback_allowed(10, media_player_id=10)


def test_external_playback_allowed_when_source_unknown() -> None:
    assert sm6_external_playback_allowed(None, media_player_id=10)


def test_plex_stop_always_clears_playlist() -> None:
    assert sm6_plex_stop_should_clear_playlist()



def test_power_on_to_idle_clears_playlist() -> None:
    assert sm6_power_transition_should_clear_playlist("ON", "IDLE")


def test_power_idle_to_on_clears_playlist() -> None:
    assert sm6_power_transition_should_clear_playlist("IDLE", "ON")


def test_power_unchanged_on_does_not_clear() -> None:
    assert not sm6_power_transition_should_clear_playlist("ON", "ON")


def test_power_first_poll_already_on_does_not_clear() -> None:
    assert not sm6_power_transition_should_clear_playlist(None, "ON")


def test_connect_rebuilds_when_on_with_playlist() -> None:
    assert sm6_connect_should_rebuild_from_playlist("ON", playlist_length=3)


def test_connect_no_rebuild_when_idle() -> None:
    assert not sm6_connect_should_rebuild_from_playlist("IDLE", playlist_length=3)


def test_connect_no_rebuild_when_playlist_empty() -> None:
    assert not sm6_connect_should_rebuild_from_playlist("ON", playlist_length=0)
