from aiohttp import ClientResponseError, RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

import pytest

from plex.dlna_browser import (
    DlnaItem,
    _artist_folder_matches,
    album_title_matches,
    discover_plex_dlna_music_ids,
    find_album,
    find_browse_section,
    find_items_by_title,
    parent_id_from_didl,
    _BY_ARTIST_SECTION_TITLES,
)


@pytest.mark.asyncio
async def test_find_items_by_title_lookup_is_case_insensitive():
    class _Browser:
        async def browse(self, object_id: str):
            return [
                DlnaItem(
                    object_id="9001",
                    title="example track b",
                    is_container=False,
                    url="http://plex.example:32469/object/9001/track.mp3",
                )
            ]

    matches = await find_items_by_title(_Browser(), {"Example Track B"})
    assert "example track b" in matches
    assert matches["example track b"].object_id == "9001"


def test_artist_folder_matches_exact_and_prefix():
    assert _artist_folder_matches("Curtis Mayfield", "Curtis Mayfield")
    assert _artist_folder_matches("Curtis Mayfield - Something", "Curtis Mayfield")
    assert not _artist_folder_matches("Mayfield", "Curtis Mayfield")


def test_album_title_matches_artist_prefixed_dlna_title():
    assert album_title_matches(
        "Curtis Mayfield - There's No Place Like America Today",
        "There's No Place Like America Today",
        artist="Curtis Mayfield",
    )


def test_parent_id_from_didl_reads_track_parent():
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="track-1" parentID="album-99" restricted="1">'
        "<dc:title>Silence</dc:title>"
        "</item></DIDL-Lite>"
    )
    assert parent_id_from_didl(didl) == "album-99"


@pytest.mark.asyncio
async def test_find_album_falls_back_when_artist_browse_raises():
    class _Browser:
        calls = 0

        async def browse(self, object_id: str, *, count: int = 200):
            _Browser.calls += 1
            if object_id == "music-root" and _Browser.calls == 1:
                request_info = RequestInfo(
                    URL("http://plex.example/control"),
                    "POST",
                    CIMultiDictProxy(CIMultiDict()),
                    URL("http://plex.example/control"),
                )
                raise ClientResponseError(request_info, (), status=500, message="Internal Server Error")
            if object_id == "music-root":
                return [DlnaItem(object_id="by-album", title="By Album", is_container=True)]
            if object_id == "by-album":
                return [
                    DlnaItem(
                        object_id="album-third",
                        title="Portishead - Third",
                        is_container=True,
                    )
                ]
            raise AssertionError(f"unexpected browse {object_id}")

    item = await find_album(
        _Browser(),
        "music-root",
        "Third",
        artist="Portishead",
    )
    assert item.object_id == "album-third"


@pytest.mark.asyncio
async def test_find_browse_section_matches_all_artists():
    class _Browser:
        async def browse(self, object_id: str, *, count: int = 50):
            return [
                DlnaItem(object_id="all-artists", title="All Artists", is_container=True),
                DlnaItem(object_id="by-album", title="By Album", is_container=True),
            ]

    item = await find_browse_section(_Browser(), "musique", *_BY_ARTIST_SECTION_TITLES)
    assert item.object_id == "all-artists"


@pytest.mark.asyncio
async def test_discover_music_ids_uses_child_containers_not_parent():
    tree = {
        "0": [
            DlnaItem("video", "Video", True),
            DlnaItem("music-root", "Music", True),
            DlnaItem("photos", "Photos", True),
        ],
        "music-root": [
            DlnaItem("musique-lib", "Musique", True),
            DlnaItem("channels", "Music Channels", True),
        ],
        "musique-lib": [
            DlnaItem("by-album", "By Album", True),
            DlnaItem("all-artists", "All Artists", True),
        ],
    }

    class _Browser:
        async def browse(self, object_id: str, *, count: int = 200):
            return tree.get(object_id, [])

    ids = await discover_plex_dlna_music_ids(_Browser())
    assert ids["music_folder_id"] == "music-root"
    assert ids["musique_id"] == "musique-lib"
