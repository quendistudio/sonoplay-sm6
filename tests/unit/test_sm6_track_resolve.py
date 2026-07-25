"""SM6 track resolution — ID-first, no blind title search."""

from types import SimpleNamespace

from plex.sm6_sync import sm6_entry_matches_track


def test_sm6_entry_matches_track_requires_title():
    entry = SimpleNamespace(title="First Light", artist="Converge", album="Jane Doe")
    track = SimpleNamespace(
        title="First Light",
        grandparentTitle="Converge",
        parentTitle="Jane Doe",
    )
    assert sm6_entry_matches_track(entry, track)


def test_sm6_entry_rejects_wrong_artist():
    entry = SimpleNamespace(title="First Light", artist="Converge", album=None)
    track = SimpleNamespace(
        title="First Light",
        grandparentTitle="Django Django",
        parentTitle="Born Under Saturn",
    )
    assert not sm6_entry_matches_track(entry, track)


def test_metadata_search_blocked_without_artist_and_duration():
    from plex.track_metadata_search import sm6_entry_metadata_search_allowed

    entry = SimpleNamespace(title="First Light", artist=None, album="Jane Doe")
    assert not sm6_entry_metadata_search_allowed(entry)
