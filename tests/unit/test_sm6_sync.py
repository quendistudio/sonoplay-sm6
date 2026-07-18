"""Unit tests for ratingKey extraction from SM6 / DLNA URIs."""

from plex.dlna_stream_cache import (
    is_plex_dlna_stream_uri,
    rating_key_from_uri,
)
from plex.sm6_sync import is_plex_resolvable_uri
from tests.fixtures.network import FAKE_HOST_IP
from tests.fixtures.plex_tracks import FAKE_TRACK_KEY_A


def test_rating_key_from_transcode_proxy_query():
    url = (
        f"http://{FAKE_HOST_IP}:32488/player/stream/transcode.mp3?"
        f"ratingKey={FAKE_TRACK_KEY_A}&device=dev-1&exp=999&sig=abc"
    )
    assert rating_key_from_uri(url) == FAKE_TRACK_KEY_A
    assert is_plex_resolvable_uri(url)


def test_rating_key_from_server_uri():
    uri = (
        "server://abc123/com.plexapp.plugins.library"
        f"/library/metadata/{FAKE_TRACK_KEY_A}"
    )
    assert rating_key_from_uri(uri) == FAKE_TRACK_KEY_A


def test_rating_key_from_dlna_object_url():
    url = "http://plex.example:32469/object/9001/track.flac"
    assert rating_key_from_uri(url) is None
    assert is_plex_dlna_stream_uri(url)
    assert is_plex_resolvable_uri(url)


def test_non_plex_uri_is_not_resolvable():
    assert not is_plex_resolvable_uri("http://radio.example/stream.mp3")
    assert rating_key_from_uri("http://radio.example/stream.mp3") is None
