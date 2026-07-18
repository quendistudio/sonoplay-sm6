"""Unit tests for Plex → SM6 queue edit planning."""

from __future__ import annotations

from dataclasses import dataclass

from plex.sm6_queue_edit import (
    Sm6ClearQueueTail,
    Sm6DeleteTrack,
    Sm6InsertTrack,
    Sm6MoveTrack,
    plan_deletes_for_removed_items,
    plan_refresh_inserts,
    reconcile_sm6_tail_to_plex,
    track_matches_entry,
)


@dataclass
class _Entry:
    track_id: int
    title: str
    artist: str | None = None
    album: str | None = None


@dataclass
class _State:
    media_queue_index: int
    tracks: tuple


@dataclass
class _Track:
    title: str
    grandparentTitle: str | None = None
    playQueueItemID: int | None = None


def test_track_matches_entry() -> None:
    entry = _Entry(1, "La Thune", "Angèle")
    assert track_matches_entry(_Track("La Thune", "Angèle"), entry)
    assert not track_matches_entry(_Track("Other", "Angèle"), entry)


def test_plan_deletes_full_tail_clear_uses_delete_all() -> None:
    ops = plan_deletes_for_removed_items(
        old_tail_item_ids=[10, 11, 12],
        new_tail_item_ids=[],
        sm6_tail_track_ids=[1, 2, 3],
    )
    assert ops == [Sm6ClearQueueTail()]


def test_plan_deletes_partial_removal() -> None:
    ops = plan_deletes_for_removed_items(
        old_tail_item_ids=[10, 11, 12],
        new_tail_item_ids=[10],
        sm6_tail_track_ids=[1, 2, 3],
    )
    assert ops == [
        Sm6DeleteTrack(3),
        Sm6DeleteTrack(2),
    ]


def test_plan_refresh_inserts_play_next_block() -> None:
    assert plan_refresh_inserts([1, 2, 3], selected_after=0) == [
        ("insert", 1),
        ("insert", 2),
        ("insert", 3),
    ]


def test_plan_refresh_inserts_append() -> None:
    assert plan_refresh_inserts([5, 6], selected_after=0) == [
        ("add", 5),
        ("add", 6),
    ]


def test_reconcile_inserts_missing_tracks() -> None:
    state = _State(
        media_queue_index=0,
        tracks=(
            _Entry(0, "A", "Artist"),
            _Entry(1, "B", "Artist"),
        ),
    )
    ops = reconcile_sm6_tail_to_plex(
        plex_tail_tracks=[_Track("B", "Artist"), _Track("C", "Artist")],
        sm6_state=state,
    )
    assert Sm6InsertTrack(insert_position=2, plex_tail_index=1) in ops


def test_reconcile_empty_plex_tail_uses_delete_all() -> None:
    state = _State(
        media_queue_index=0,
        tracks=(
            _Entry(0, "A", "Artist"),
            _Entry(1, "B", "Artist"),
        ),
    )
    ops = reconcile_sm6_tail_to_plex(plex_tail_tracks=[], sm6_state=state)
    assert ops == [Sm6ClearQueueTail()]


def test_reconcile_deletes_orphan_sm6_tracks() -> None:
    state = _State(
        media_queue_index=0,
        tracks=(
            _Entry(0, "A", "Artist"),
            _Entry(1, "B", "Artist"),
            _Entry(2, "C", "Artist"),
        ),
    )
    ops = reconcile_sm6_tail_to_plex(
        plex_tail_tracks=[_Track("B", "Artist")],
        sm6_state=state,
    )
    assert Sm6DeleteTrack(2) in ops
    assert Sm6DeleteTrack(1) not in ops


def test_reconcile_move_reorders_tail() -> None:
    state = _State(
        media_queue_index=0,
        tracks=(
            _Entry(0, "A", "Artist"),
            _Entry(1, "B", "Artist"),
            _Entry(2, "C", "Artist"),
        ),
    )
    ops = reconcile_sm6_tail_to_plex(
        plex_tail_tracks=[
            _Track("C", "Artist"),
            _Track("B", "Artist"),
        ],
        sm6_state=state,
    )
    assert any(isinstance(op, Sm6MoveTrack) for op in ops)
