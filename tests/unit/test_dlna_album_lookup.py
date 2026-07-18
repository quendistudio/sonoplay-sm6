from plex.dlna_browser import DlnaItem, _artist_folder_matches, album_title_matches, find_items_by_title
import pytest


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
