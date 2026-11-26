"""Unit tests for DLNA stream cache entries."""

from plex.dlna_stream_cache import StreamCacheEntry
from tests.fixtures.plex_tracks import hi_res_flac_track


def test_stream_cache_entry_matches_track_metadata():
    track = hi_res_flac_track(title="Example Track A", album="Example Hi-Res Album")
    entry = StreamCacheEntry.from_track(
        track,
        "http://plex.example:32469/object/abc/track.flac",
        object_id="abc",
    )
    assert entry.matches_track(track)
    other = hi_res_flac_track(title="Other", album="Example Hi-Res Album")
    assert not entry.matches_track(other)


def test_stream_cache_entry_from_legacy_string():
    entry = StreamCacheEntry.from_raw("http://example/object/x/y")
    assert entry is not None
    assert entry.url.endswith("/y")


def test_from_track_tolerates_dotmap_duration():
    from dotmap import DotMap
    from types import SimpleNamespace

    track = SimpleNamespace(
        title="Example Track A",
        parentTitle="Example Hi-Res Album",
        grandparentTitle="Example Artist",
        parentIndex=DotMap(),
        duration=DotMap(),
    )
    entry = StreamCacheEntry.from_track(track, "http://example/object/x/y")
    assert entry.duration_ms is None
    assert entry.parent_index is None
    assert entry.matches_track(track)
