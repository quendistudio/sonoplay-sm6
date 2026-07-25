"""Tests Plex enqueue → SM6 QueueFolder action mapping."""

import importlib.util
import sys
import types
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

_dlna_pkg = types.ModuleType("dlna")
_dlna_pkg.__path__ = [str(_root / "dlna")]
sys.modules.setdefault("dlna", _dlna_pkg)

_spec = importlib.util.spec_from_file_location(
    "dlna.sm6_queue",
    _root / "dlna" / "sm6_queue.py",
)
sm6_queue = importlib.util.module_from_spec(_spec)
sys.modules["dlna.sm6_queue"] = sm6_queue
assert _spec.loader is not None
_spec.loader.exec_module(sm6_queue)

sm6_action_for_enqueue = sm6_queue.sm6_action_for_enqueue
initial_sm6_queue_action = sm6_queue.initial_sm6_queue_action
plan_refresh_enqueue_actions = sm6_queue.plan_refresh_enqueue_actions


def test_enqueue_mapping() -> None:
    assert sm6_action_for_enqueue("replace") == "REPLACE"
    assert sm6_action_for_enqueue("add") == "APPEND"
    assert sm6_action_for_enqueue("next") == "PLAY_NEXT"
    assert sm6_action_for_enqueue("play") == "PLAY_NOW"


def test_initial_queue_action_replace_for_transcode_playlist() -> None:
    action = initial_sm6_queue_action(
        segment_kind="track",
        replace_transcode_queue=True,
    )
    assert action == "REPLACE"


def test_initial_queue_action_replace_for_playlist_takeover() -> None:
    action = initial_sm6_queue_action(
        segment_kind="track",
        replace_playlist_queue=True,
    )
    assert action == "REPLACE"


def test_initial_queue_action_play_for_single_track() -> None:
    assert initial_sm6_queue_action(segment_kind="track") == "PLAY_NOW"


def test_refresh_append_at_queue_tail() -> None:
    # selected=0 (A*), new tracks at indices 3,4 → add to queue
    assert plan_refresh_enqueue_actions([3, 4], selected_after=0) == ["add", "add"]


def test_refresh_play_next_single() -> None:
    assert plan_refresh_enqueue_actions([1], selected_after=0) == ["next"]


def test_refresh_play_next_album_block() -> None:
    assert plan_refresh_enqueue_actions([1, 2, 3], selected_after=0) == [
        "next",
        "next",
        "next",
    ]


def test_refresh_non_contiguous_not_play_next() -> None:
    assert plan_refresh_enqueue_actions([1, 3], selected_after=0) == ["add", "add"]


# InsertPlaylistTrack planning lives in plex.sm6_queue_edit (see test_sm6_queue_edit.py).
