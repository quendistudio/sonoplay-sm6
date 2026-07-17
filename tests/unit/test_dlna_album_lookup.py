from plex.dlna_browser import _artist_folder_matches, album_title_matches


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
