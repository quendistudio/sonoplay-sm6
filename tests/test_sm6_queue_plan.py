"""Tests SM6 queue segment planning (album container batching)."""

from types import SimpleNamespace

from plex.sm6_queue_plan import (
    classify_sm6_play_mode_light,
    detect_sm6_play_mode_from_tracks,
    is_full_album_run,
    plan_queue_segments,
)


def _track(parent_key: str, parent_index: int, title: str = "t") -> SimpleNamespace:
    return SimpleNamespace(
        parentRatingKey=parent_key,
        parentIndex=parent_index,
        title=title,
        ratingKey=f"{parent_key}-{parent_index}",
    )


def test_full_album_run_detected() -> None:
    run = [_track("album1", i) for i in range(1, 4)]
    assert is_full_album_run(run, 3) is True


def test_partial_album_run_rejected() -> None:
    run = [_track("album1", i) for i in range(3, 5)]
    assert is_full_album_run(run, 10) is False


def test_wrong_order_rejected() -> None:
    run = [_track("album1", 2), _track("album1", 1), _track("album1", 3)]
    assert is_full_album_run(run, 3) is False


def test_plan_groups_two_full_albums() -> None:
    tracks = [_track("a1", i) for i in range(1, 3)] + [_track("a2", i) for i in range(1, 4)]
    segments = plan_queue_segments(tracks, {"a1": 2, "a2": 3})
    assert [segment.kind for segment in segments] == ["album", "album"]
    assert segments[0].parent_rating_key == "a1"
    assert len(segments[0].tracks) == 2
    assert segments[1].parent_rating_key == "a2"


def test_plan_partial_album_falls_back_to_tracks() -> None:
    tracks = [_track("a1", i) for i in range(5, 8)]
    segments = plan_queue_segments(tracks, {"a1": 10})
    assert len(segments) == 3
    assert all(segment.kind == "track" for segment in segments)


def test_plan_mixed_partial_then_full_album() -> None:
    tracks = [_track("a1", 3), _track("a1", 4)] + [_track("a2", i) for i in range(1, 3)]
    segments = plan_queue_segments(tracks, {"a1": 10, "a2": 2})
    assert [segment.kind for segment in segments] == ["track", "track", "album"]


def test_detect_play_mode_album_after_full_pagination() -> None:
    tracks = [_track("album1", i) for i in range(1, 5)]
    assert detect_sm6_play_mode_from_tracks(tracks) == "album"


def test_detect_play_mode_track_when_only_one_loaded() -> None:
    assert detect_sm6_play_mode_from_tracks([_track("album1", 1)]) == "track"


def test_detect_play_mode_playlist_for_multi_album() -> None:
    tracks = [_track("a1", 1), _track("a2", 1)]
    assert detect_sm6_play_mode_from_tracks(tracks) == "playlist"


def _light(
    *,
    queue_total: int,
    loaded_tracks: list,
    album_leaf_count: int | None,
    selected_track=None,
) -> str | None:
    if selected_track is None and loaded_tracks:
        selected_track = loaded_tracks[0]
    return classify_sm6_play_mode_light(
        queue_total=queue_total,
        selected_track=selected_track,
        loaded_tracks=loaded_tracks,
        album_leaf_count=album_leaf_count,
    )


def test_classify_light_full_album() -> None:
    loaded = [_track("album1", i) for i in range(1, 4)]
    assert _light(queue_total=9, loaded_tracks=loaded, album_leaf_count=9) == "album"


def test_classify_light_partial_album() -> None:
    loaded = [_track("album1", i) for i in range(1, 4)]
    assert _light(queue_total=3, loaded_tracks=loaded, album_leaf_count=9) == "playlist"


def test_classify_light_multi_album() -> None:
    loaded = [_track("a1", 1), _track("a2", 1)]
    assert _light(queue_total=2, loaded_tracks=loaded, album_leaf_count=9) == "playlist"


def test_classify_light_single_track() -> None:
    loaded = [_track("album1", 1)]
    assert _light(queue_total=1, loaded_tracks=loaded, album_leaf_count=9) == "track"


def test_classify_light_ambiguous_without_leaf_count() -> None:
    loaded = [_track("album1", i) for i in range(1, 4)]
    assert _light(queue_total=3, loaded_tracks=loaded, album_leaf_count=None) is None
