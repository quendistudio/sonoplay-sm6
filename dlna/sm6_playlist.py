"""SM6 queue read SOAP (UuVolControl:5, RecivaRadio/invoke)."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

from dlna.sm6_queue import reciva_radio_invoke_url

_UUVOL_CONTROL_NS = "urn:UuVol-com:service:UuVolControl:5"

GET_PLAYLIST_LENGTH_ACTION = f'"{_UUVOL_CONTROL_NS}#GetPlaylistLength"'
GET_PLAYLIST_TRACK_DETAILS_ACTION = f'"{_UUVOL_CONTROL_NS}#GetPlaylistTrackDetails"'
GET_MEDIA_QUEUE_INDEX_ACTION = f'"{_UUVOL_CONTROL_NS}#GetMediaQueueIndex"'
GET_CURRENT_PLAYLIST_TRACK_ACTION = f'"{_UUVOL_CONTROL_NS}#GetCurrentPlaylistTrack"'
SET_CURRENT_PLAYLIST_TRACK_ACTION = f'"{_UUVOL_CONTROL_NS}#SetCurrentPlaylistTrack"'

_PLAYLIST_LENGTH = re.compile(r"<PlaylistLength>(\d+)</PlaylistLength>", re.I)
_MEDIA_QUEUE_INDEX = re.compile(r"<MediaQueueIndex>(-?\d+)</MediaQueueIndex>", re.I)
_CURRENT_TRACK_ID = re.compile(r"<CurrentPlaylistTrackID>(\d+)</CurrentPlaylistTrackID>", re.I)
_TRACKS_XML = re.compile(r"<TracksXML>(.*?)</TracksXML>", re.I | re.S)
_PLAYLIST_ENTRY = re.compile(r"<playlist-entry\s+id=\"(\d+)\">(.*?)</playlist-entry>", re.I | re.S)
_TAG = re.compile(r"<([a-z-]+)>(.*?)</\1>", re.I | re.S)


@dataclass(frozen=True)
class Sm6PlaylistEntry:
    track_id: int
    title: str
    artist: str | None = None
    album: str | None = None
    duration_seconds: int | None = None


@dataclass(frozen=True)
class Sm6PlaylistState:
    length: int
    current_track_id: int
    media_queue_index: int
    tracks: tuple[Sm6PlaylistEntry, ...]


def _soap_body(inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<s:Body>{inner}</s:Body></s:Envelope>"
    )


def build_get_playlist_length_body() -> str:
    return _soap_body(f'<u:GetPlaylistLength xmlns:u="{_UUVOL_CONTROL_NS}"/>')


def build_get_playlist_track_details_body(*, start_track_id: int, track_count: int) -> str:
    return _soap_body(
        f'<u:GetPlaylistTrackDetails xmlns:u="{_UUVOL_CONTROL_NS}">'
        f"<StartTrackID>{start_track_id}</StartTrackID>"
        f"<TrackCount>{track_count}</TrackCount>"
        "</u:GetPlaylistTrackDetails>"
    )


def build_get_media_queue_index_body() -> str:
    return _soap_body(f'<u:GetMediaQueueIndex xmlns:u="{_UUVOL_CONTROL_NS}"/>')


def build_get_current_playlist_track_body() -> str:
    return _soap_body(f'<u:GetCurrentPlaylistTrack xmlns:u="{_UUVOL_CONTROL_NS}"/>')


def sm6_set_current_playlist_track_id(
    queue_index: int,
    entry: Sm6PlaylistEntry | None = None,
) -> int:
    """Map 0-based queue position to CurrentPlaylistTrackID for SetCurrentPlaylistTrack.

    GetMediaQueueIndex is 0-based; GetCurrentPlaylistTrack returns index + 1 on SM6.
    """
    if entry is not None and entry.track_id != queue_index:
        return entry.track_id
    return queue_index + 1


def build_set_current_playlist_track_body(*, track_id: int) -> str:
    return _soap_body(
        f'<u:SetCurrentPlaylistTrack xmlns:u="{_UUVOL_CONTROL_NS}">'
        f"<CurrentPlaylistTrackID>{track_id}</CurrentPlaylistTrackID>"
        "</u:SetCurrentPlaylistTrack>"
    )


def parse_playlist_length(xml: str) -> int:
    match = _PLAYLIST_LENGTH.search(xml)
    if not match:
        return 0
    return int(match.group(1))


def parse_media_queue_index(xml: str) -> int:
    match = _MEDIA_QUEUE_INDEX.search(xml)
    if not match:
        return -1
    return int(match.group(1))


def parse_current_playlist_track_id(xml: str) -> int:
    match = _CURRENT_TRACK_ID.search(xml)
    if not match:
        return 0
    return int(match.group(1))


def parse_playlist_track_details(xml: str) -> list[Sm6PlaylistEntry]:
    match = _TRACKS_XML.search(xml)
    if not match:
        return []
    raw = html.unescape(match.group(1))
    entries: list[Sm6PlaylistEntry] = []
    for entry_match in _PLAYLIST_ENTRY.finditer(raw):
        track_id = int(entry_match.group(1))
        block = entry_match.group(2)
        fields = {name: html.unescape(value).strip() for name, value in _TAG.findall(block)}
        duration = fields.get("duration")
        entries.append(
            Sm6PlaylistEntry(
                track_id=track_id,
                title=fields.get("title", ""),
                artist=fields.get("artist"),
                album=fields.get("album"),
                duration_seconds=int(duration) if duration and duration.isdigit() else None,
            )
        )
    return entries
