"""Unit tests for PlayQueue track key matching."""

from unittest.mock import MagicMock

import pytest

from plex.play_queue import PlayQueue


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
