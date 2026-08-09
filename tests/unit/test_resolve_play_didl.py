"""Unit tests for Plex DLNA resolve_play_didl helpers."""
from types import SimpleNamespace

from plex.url_resolver import album_rating_key_from_track, track_skip_count


def test_track_skip_count_from_index():
    track = SimpleNamespace(index=3, parentIndex=1)
    assert track_skip_count(track) == 2


def test_track_skip_count_first_track():
    track = SimpleNamespace(index=1, parentIndex=1)
    assert track_skip_count(track) == 0


def test_track_skip_count_prefers_index_over_disc_parent_index():
    # parentIndex is disc # — must not win over track index.
    track = SimpleNamespace(index=8, parentIndex=1)
    assert track_skip_count(track) == 7


def test_track_skip_count_legacy_parent_index_fallback():
    track = SimpleNamespace(parentIndex=3)
    assert track_skip_count(track) == 2


def test_track_skip_count_missing_index():
    track = SimpleNamespace()
    assert track_skip_count(track) == 0


def test_album_rating_key_from_track():
    track = SimpleNamespace(parentRatingKey=38800)
    assert album_rating_key_from_track(track) == "38800"


def test_album_rating_key_from_track_empty():
    track = SimpleNamespace(parentRatingKey="  ")
    assert album_rating_key_from_track(track) is None


def test_resolve_play_didl_uses_album_container_with_play_from_id(monkeypatch):
    import asyncio

    from plex.url_resolver import UrlResolver

    track = SimpleNamespace(
        ratingKey="38834",
        parentRatingKey="38820",
        parentTitle="There's No Place Like America Today",
        grandparentTitle="Curtis Mayfield",
        index=5,
        parentIndex=1,
        title="Billy Jack",
    )
    resolver = UrlResolver("http://example/dlna")

    async def fake_resolve_didl(item, *, media_kind="track"):
        if media_kind == "album":
            assert item.ratingKey == "38820"
            assert item.title == "There's No Place Like America Today"
            assert item.parentTitle == "Curtis Mayfield"
            return "<album-didl/>"
        raise AssertionError("track DIDL should not be used when album key is present")

    async def fake_ensure_ids():
        return None

    async def fake_object_id(_track):
        return "a02fb6b17977363ff81f"

    monkeypatch.setattr(resolver, "resolve_didl", fake_resolve_didl)
    monkeypatch.setattr(resolver, "_ensure_dlna_ids", fake_ensure_ids)
    monkeypatch.setattr(resolver, "resolve_track_object_id", fake_object_id)

    didl, play_from_id, skip = asyncio.run(
        resolver.resolve_play_didl(track, start_playback=True),
    )
    assert didl == "<album-didl/>"
    assert play_from_id == "a02fb6b17977363ff81f"
    assert skip == 0


def test_resolve_play_didl_falls_back_to_skip_without_object_id(monkeypatch):
    import asyncio

    from plex.url_resolver import UrlResolver

    track = SimpleNamespace(
        ratingKey="38834",
        parentRatingKey="38820",
        parentTitle="There's No Place Like America Today",
        grandparentTitle="Curtis Mayfield",
        index=5,
        parentIndex=1,
        title="Billy Jack",
    )
    resolver = UrlResolver("http://example/dlna")

    async def fake_resolve_didl(item, *, media_kind="track"):
        if media_kind == "album":
            return "<album-didl/>"
        raise AssertionError("unexpected track DIDL")

    async def fake_object_id(_track):
        return None

    async def fake_ensure_ids():
        return None

    monkeypatch.setattr(resolver, "resolve_didl", fake_resolve_didl)
    monkeypatch.setattr(resolver, "_ensure_dlna_ids", fake_ensure_ids)
    monkeypatch.setattr(resolver, "resolve_track_object_id", fake_object_id)

    didl, play_from_id, skip = asyncio.run(
        resolver.resolve_play_didl(track, start_playback=True),
    )
    assert didl == "<album-didl/>"
    assert play_from_id is None
    assert skip == 4


def test_playlist_track_resolves_to_track_didl_not_album(monkeypatch):
    import asyncio

    from plex.url_resolver import UrlResolver

    track = SimpleNamespace(
        ratingKey="34512",
        parentRatingKey="34509",
        parentTitle="Evil Empire",
        grandparentTitle="Rage Against the Machine",
        parentIndex=3,
    )
    resolver = UrlResolver("http://example/dlna")

    async def fake_resolve_didl(item, *, media_kind="track"):
        if media_kind == "track":
            assert item.ratingKey == "34512"
            return "<track-didl/>"
        raise AssertionError("album DIDL should not be used for playlist items")

    async def fake_ensure_ids():
        return None

    monkeypatch.setattr(resolver, "resolve_didl", fake_resolve_didl)
    monkeypatch.setattr(resolver, "_ensure_dlna_ids", fake_ensure_ids)

    didl, play_from_id, skip = asyncio.run(
        resolver.resolve_play_didl(track, start_playback=True, prefer_track_didl=True),
    )
    assert didl == "<track-didl/>"
    assert play_from_id is None
    assert skip == 0
