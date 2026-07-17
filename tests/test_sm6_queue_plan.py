"""Tests SM6 queue segment planning (album container batching)."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "plex.sm6_queue_plan",
    _root / "plex" / "sm6_queue_plan.py",
)
sm6_queue_plan = importlib.util.module_from_spec(_spec)
sys.modules["plex.sm6_queue_plan"] = sm6_queue_plan
assert _spec.loader is not None
_spec.loader.exec_module(sm6_queue_plan)

is_full_album_run = sm6_queue_plan.is_full_album_run
plan_queue_segments = sm6_queue_plan.plan_queue_segments


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
