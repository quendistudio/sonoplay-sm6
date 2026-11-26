"""Shared test doubles for SM6 SOAP and Plex playQueue boundaries."""

from tests.fakes.plex import FakePlayQueue, FakePlexLib, make_album_tracks
from tests.fakes.sm6 import FakeSm6, fake_dlna_device

__all__ = [
    "FakeSm6",
    "FakePlayQueue",
    "FakePlexLib",
    "fake_dlna_device",
    "make_album_tracks",
]
