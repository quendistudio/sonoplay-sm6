"""Tests SM6 queue read SOAP parsers."""

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
_sm6_queue_stub = types.ModuleType("dlna.sm6_queue")
_sm6_queue_stub.reciva_radio_invoke_url = lambda description_url: description_url
sys.modules["dlna.sm6_queue"] = _sm6_queue_stub

_spec = importlib.util.spec_from_file_location(
    "dlna.sm6_playlist",
    _root / "dlna" / "sm6_playlist.py",
)
sm6_playlist = importlib.util.module_from_spec(_spec)
sys.modules["dlna.sm6_playlist"] = sm6_playlist
assert _spec.loader is not None
_spec.loader.exec_module(sm6_playlist)

parse_current_playlist_track_id = sm6_playlist.parse_current_playlist_track_id
parse_media_queue_index = sm6_playlist.parse_media_queue_index
parse_playlist_length = sm6_playlist.parse_playlist_length
parse_playlist_track_details = sm6_playlist.parse_playlist_track_details
sm6_set_current_playlist_track_id = sm6_playlist.sm6_set_current_playlist_track_id
Sm6PlaylistEntry = sm6_playlist.Sm6PlaylistEntry

# Extrait de plexupnp/.temp/playlist.txt (GetPlaylistTrackDetails)
PLAYLIST_TRACK_DETAILS_RESPONSE = """<?xml version="1.0" ?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><r:GetPlaylistTrackDetailsResponse xmlns:r="urn:UuVol-com:service:UuVolControl:5"><TracksXML>&lt;reciva&gt;&lt;playlist start="0" count="16" total="12"&gt;&lt;playlist-entry id="0"&gt;&lt;artist&gt;Angèle&lt;/artist&gt;
&lt;album&gt;Brol&lt;/album&gt;
&lt;genre&gt;Unknown&lt;/genre&gt;
&lt;duration&gt;202&lt;/duration&gt;
&lt;title&gt;La Thune&lt;/title&gt;
&lt;/playlist-entry&gt;
&lt;playlist-entry id="1"&gt;&lt;artist&gt;Angèle&lt;/artist&gt;
&lt;album&gt;Brol&lt;/album&gt;
&lt;duration&gt;189&lt;/duration&gt;
&lt;title&gt;Balance ton quoi&lt;/title&gt;
&lt;/playlist-entry&gt;
&lt;/playlist&gt;&lt;/reciva&gt;</TracksXML></r:GetPlaylistTrackDetailsResponse></s:Body></s:Envelope>"""

CURRENT_TRACK_RESPONSE = """<CurrentPlaylistTrackID>3</CurrentPlaylistTrackID>"""
QUEUE_INDEX_RESPONSE = """<MediaQueueIndex>2</MediaQueueIndex>"""
LENGTH_RESPONSE = """<PlaylistLength>12</PlaylistLength>"""


def test_parse_playlist_track_details():
    entries = parse_playlist_track_details(PLAYLIST_TRACK_DETAILS_RESPONSE)
    assert len(entries) == 2
    assert entries[0].track_id == 0
    assert entries[0].title == "La Thune"
    assert entries[0].artist == "Angèle"
    assert entries[0].album == "Brol"
    assert entries[0].duration_seconds == 202
    assert entries[1].track_id == 1
    assert entries[1].title == "Balance ton quoi"
    assert entries[1].duration_seconds == 189


def test_parse_playlist_scalar_fields():
    assert parse_playlist_length(LENGTH_RESPONSE) == 12
    assert parse_media_queue_index(QUEUE_INDEX_RESPONSE) == 2
    assert parse_current_playlist_track_id(CURRENT_TRACK_RESPONSE) == 3


def test_build_set_current_playlist_track_body() -> None:
    body = sm6_playlist.build_set_current_playlist_track_body(track_id=3)
    assert "SetCurrentPlaylistTrack" in body
    assert "<CurrentPlaylistTrackID>3</CurrentPlaylistTrackID>" in body
    assert sm6_playlist.SET_CURRENT_PLAYLIST_TRACK_ACTION.endswith("#SetCurrentPlaylistTrack\"")


def test_sm6_set_current_playlist_track_id_one_based() -> None:
    entry = Sm6PlaylistEntry(track_id=8, title="It Was Love That We Needed")
    assert sm6_set_current_playlist_track_id(8, entry) == 9
    assert sm6_set_current_playlist_track_id(0, Sm6PlaylistEntry(track_id=0, title="First")) == 1


def test_sm6_set_current_playlist_track_id_non_contiguous_entry() -> None:
    entry = Sm6PlaylistEntry(track_id=42, title="Custom")
    assert sm6_set_current_playlist_track_id(3, entry) == 42
