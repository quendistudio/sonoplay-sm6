"""Unit tests for PlayQueue track key matching."""

import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest


def _make_stub(name):
    mod = types.ModuleType(name)
    mod.__path__ = []
    mod.__file__ = f"<stub {name}>"
    mod.__getattr__ = lambda attr: MagicMock()
    return mod


for _name in ["dotmap", "starlette", "starlette.datastructures", "utils"]:
    if _name not in sys.modules:
        sys.modules[_name] = _make_stub(_name)

sys.modules["utils"].g = MagicMock()

sys.modules.pop("plex.play_queue", None)
play_queue = importlib.import_module("plex.play_queue")
PlayQueue = play_queue.PlayQueue


@pytest.fixture
def queue():
    q = object.__new__(PlayQueue)
    q.start_offset = 0
    return q


def _track(key="/library/metadata/100", rating_key=100, title="Song"):
    track = MagicMock()
    track.key = key
    track.ratingKey = rating_key
    track.title = title
    return track


def test_track_matches_key_by_metadata_path(queue):
    track = _track(key="/library/metadata/900001", rating_key=900001)
    assert queue._track_matches_key(track, "/library/metadata/900001")


def test_track_matches_key_by_rating_key_only(queue):
    track = _track(key="/library/metadata/900001", rating_key=900001)
    assert queue._track_matches_key(track, "/library/metadata/900001/")


def test_track_does_not_match_different_rating_key(queue):
    track = _track(key="/library/metadata/900002", rating_key=900002, title="Example Track B")
    assert not queue._track_matches_key(track, "/library/metadata/900001")
