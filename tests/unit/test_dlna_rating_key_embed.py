"""Unit tests for ratingKey embedding in DLNA stream URLs and DIDL."""

from plex.dlna_stream_cache import (
    embed_rating_key_in_stream_url,
    normalize_stream_url,
    patch_didl_res_urls,
    rating_key_from_uri,
)
from tests.fixtures.plex_tracks import FAKE_TRACK_KEY_A


def test_embed_rating_key_appends_query_param():
    url = "http://plex.example:32469/object/9001/track.flac"
    tagged = embed_rating_key_in_stream_url(url, FAKE_TRACK_KEY_A)
    assert tagged.endswith(f"ratingKey={FAKE_TRACK_KEY_A}")
    assert rating_key_from_uri(tagged) == FAKE_TRACK_KEY_A


def test_embed_rating_key_preserves_existing_query():
    url = "http://plex.example:32469/object/9001/track.flac?foo=bar"
    tagged = embed_rating_key_in_stream_url(url, FAKE_TRACK_KEY_A)
    assert "foo=bar" in tagged
    assert f"ratingKey={FAKE_TRACK_KEY_A}" in tagged


def test_embed_rating_key_idempotent():
    url = f"http://plex.example:32469/object/9001/track.flac?ratingKey={FAKE_TRACK_KEY_A}"
    assert embed_rating_key_in_stream_url(url, "99999") == url


def test_normalize_stream_url_strips_rating_key():
    raw = "http://plex.example:32469/object/9001/track.flac"
    tagged = f"{raw}?ratingKey={FAKE_TRACK_KEY_A}&foo=bar"
    assert normalize_stream_url(tagged) == f"{raw}?foo=bar"
    assert normalize_stream_url(raw) == raw


def test_patch_didl_res_urls_single_track():
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="9001" parentID="0" restricted="1">'
        "<dc:title>Example</dc:title>"
        '<res protocolInfo="http-get:*:audio/flac:*">'
        "http://plex.example:32469/object/9001/track.flac"
        "</res>"
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        "</item></DIDL-Lite>"
    )
    patched = patch_didl_res_urls(didl, {"9001": FAKE_TRACK_KEY_A})
    assert f"ratingKey={FAKE_TRACK_KEY_A}" in patched
    assert (
        rating_key_from_uri(
            f"http://plex.example:32469/object/9001/track.flac?ratingKey={FAKE_TRACK_KEY_A}"
        )
        == FAKE_TRACK_KEY_A
    )


def test_patch_didl_res_urls_album_mapping():
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="100" parentID="0" restricted="1">'
        '<res protocolInfo="http-get:*:audio/flac:*">'
        "http://plex.example:32469/object/a/track.flac"
        "</res></item>"
        '<item id="101" parentID="0" restricted="1">'
        '<res protocolInfo="http-get:*:audio/flac:*">'
        "http://plex.example:32469/object/b/track.flac"
        "</res></item>"
        "</DIDL-Lite>"
    )
    patched = patch_didl_res_urls(didl, {"a": "111", "b": "222"})
    assert "object/a/track.flac?ratingKey=111" in patched
    assert "object/b/track.flac?ratingKey=222" in patched
