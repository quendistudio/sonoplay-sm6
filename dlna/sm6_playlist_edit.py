"""SM6 playlist edit SOAP (Insert/Delete/Move — UuVolControl:5, RecivaRadio/invoke)."""
from __future__ import annotations

import xml.sax.saxutils

from dlna.sm6_playlist import _UUVOL_CONTROL_NS, _soap_body

INSERT_PLAYLIST_TRACK_ACTION = f'"{_UUVOL_CONTROL_NS}#InsertPlaylistTrack"'
DELETE_PLAYLIST_TRACK_ACTION = f'"{_UUVOL_CONTROL_NS}#DeletePlaylistTrack"'
MOVE_PLAYLIST_TRACK_ACTION = f'"{_UUVOL_CONTROL_NS}#MovePlaylistTrack"'


def build_insert_playlist_track_body(*, insert_position: int, didl: str) -> str:
    escaped_didl = xml.sax.saxutils.escape(didl)
    return _soap_body(
        f'<u:InsertPlaylistTrack xmlns:u="{_UUVOL_CONTROL_NS}">'
        f"<InsertPosition>{insert_position}</InsertPosition>"
        f"<TrackData>{escaped_didl}</TrackData>"
        "</u:InsertPlaylistTrack>"
    )


def build_delete_playlist_track_body(*, playlist_track_id: int) -> str:
    return _soap_body(
        f'<u:DeletePlaylistTrack xmlns:u="{_UUVOL_CONTROL_NS}">'
        f"<PlaylistTrackID>{playlist_track_id}</PlaylistTrackID>"
        "</u:DeletePlaylistTrack>"
    )


def build_move_playlist_track_body(*, from_index: int, to_index: int) -> str:
    return _soap_body(
        f'<u:MovePlaylistTrack xmlns:u="{_UUVOL_CONTROL_NS}">'
        f"<FromIndex>{from_index}</FromIndex>"
        f"<ToIndex>{to_index}</ToIndex>"
        "</u:MovePlaylistTrack>"
    )
