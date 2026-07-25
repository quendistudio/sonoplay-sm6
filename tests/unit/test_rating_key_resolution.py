"""Unit tests for ratingKey resolution priority from SM6 TrackURIs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plex.dlna_stream_cache import (
    rating_key_from_pms_uri,
    rating_key_from_query_param,
    rating_key_from_sonoplay_transcode_object,
    rating_key_from_uri,
)
from plex.sm6_sync import rating_key_for_polled_uri
from tests.fixtures.plex_tracks import FAKE_TRACK_KEY_A


def test_query_param_beats_pms_path_when_both_present():
    uri = (
        f"server://abc/com.plexapp.plugins.library/library/metadata/99999"
        f"?ratingKey={FAKE_TRACK_KEY_A}"
    )
    assert rating_key_from_query_param(uri) == FAKE_TRACK_KEY_A
    assert rating_key_from_uri(uri) == FAKE_TRACK_KEY_A


def test_pms_uri_is_first_fallback_after_query_param():
    uri = f"server://abc/com.plexapp.plugins.library/library/metadata/{FAKE_TRACK_KEY_A}"
    assert rating_key_from_query_param(uri) is None
    assert rating_key_from_pms_uri(uri) == FAKE_TRACK_KEY_A
    assert rating_key_from_uri(uri) == FAKE_TRACK_KEY_A


def test_sonoplay_object_is_third_embedded_tier():
    uri = "http://host/object/sonoplay-tc-900001/track.mp3"
    assert rating_key_from_query_param(uri) is None
    assert rating_key_from_pms_uri(uri) is None
    assert rating_key_from_sonoplay_transcode_object(uri) == "900001"


@pytest.mark.asyncio
async def test_rating_key_for_polled_uri_order(monkeypatch):
    adapter = SimpleNamespace(queue=None)
    uri = f"server://abc/com.plexapp.plugins.library/library/metadata/{FAKE_TRACK_KEY_A}"

    resolver = MagicMock()
    resolver.rating_key_from_dlna_cache_only = MagicMock(return_value="111111")
    monkeypatch.setattr(
        "plex.url_resolver.get_url_resolver",
        lambda: resolver,
    )

    assert await rating_key_for_polled_uri(adapter, uri) == FAKE_TRACK_KEY_A
    resolver.rating_key_from_dlna_cache_only.assert_not_called()


@pytest.mark.asyncio
async def test_rating_key_for_polled_uri_falls_back_to_dlna_cache(monkeypatch):
    adapter = SimpleNamespace(queue=None)
    uri = "http://plex.example:32469/object/9001/track.flac"

    resolver = MagicMock()
    resolver.rating_key_from_dlna_cache_only = MagicMock(return_value="222222")
    monkeypatch.setattr(
        "plex.url_resolver.get_url_resolver",
        lambda: resolver,
    )

    assert await rating_key_for_polled_uri(adapter, uri) == "222222"
    resolver.rating_key_from_dlna_cache_only.assert_called_once_with(uri)
