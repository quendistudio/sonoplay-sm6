"""Strict metadata fallback helpers."""

from types import SimpleNamespace

from plex.track_metadata_search import (
    disambiguate_tracks_by_duration,
    pick_unique_track,
    sm6_entry_metadata_search_allowed,
)


def test_metadata_search_requires_artist_and_album_or_duration():
    assert not sm6_entry_metadata_search_allowed(SimpleNamespace(title="X", artist=None, album="A"))
    assert not sm6_entry_metadata_search_allowed(SimpleNamespace(title="X", artist="A", album=None))
    assert sm6_entry_metadata_search_allowed(SimpleNamespace(title="X", artist="A", album="Album"))
    assert sm6_entry_metadata_search_allowed(
        SimpleNamespace(title="X", artist="A", album=None, duration_seconds=240),
    )


def test_disambiguate_same_title_on_album_by_duration():
    candidates = [
        {"title": "Intro", "ratingKey": "1", "duration": 120_000},
        {"title": "Intro", "ratingKey": "2", "duration": 240_000},
    ]
    matched = disambiguate_tracks_by_duration(candidates, duration_seconds=240)
    assert len(matched) == 1
    assert matched[0]["ratingKey"] == "2"


def test_pick_unique_track_refuses_ambiguous_homonyms():
    candidates = [
        {"title": "Every Breath You Take", "ratingKey": "1", "duration": 200_000},
        {"title": "Every Breath You Take", "ratingKey": "2", "duration": 210_000},
    ]
    assert pick_unique_track(candidates, label="homonym") is None
