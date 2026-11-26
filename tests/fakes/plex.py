"""Fake Plex playQueue / library boundary for adapter scenarios."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from tests.fixtures.plex_tracks import FAKE_TRACK_KEY_A, hi_res_flac_track


def make_album_tracks(
    *,
    count: int = 3,
    album: str = "Example Album",
    artist: str = "Example Artist",
    parent_key: str = "900100",
    start_key: int = 900001,
) -> list[SimpleNamespace]:
    tracks = []
    for i in range(count):
        rk = str(start_key + i)
        track = hi_res_flac_track(
            title=f"Track {i + 1}",
            album=album,
            artist=artist,
            rating_key=rk,
            sample_rate="44100",
            bitrate="1411",
        )
        track.parentRatingKey = parent_key
        track.index = i + 1
        track.key = f"/library/metadata/{rk}"
        track.playQueueItemID = 1000 + i
        tracks.append(track)
    return tracks


class FakePlayQueue:
    def __init__(
        self,
        tracks: list[Any] | None = None,
        *,
        container_key: str = "/playQueues/1",
        play_queue_id: int = 1,
        selected: int = 0,
    ) -> None:
        self.tracks = list(tracks or make_album_tracks())
        self.container_key = container_key
        self._selected = selected
        self.info = SimpleNamespace(
            playQueueID=play_queue_id,
            playQueueSelectedItemOffset=selected,
            playQueueTotalCount=len(self.tracks),
            playQueueVersion=1,
        )
        self.get_info = AsyncMock(side_effect=self._get_info)
        self.select_track_key = AsyncMock(side_effect=self._select_track_key)
        self.set_selected_offset = AsyncMock(side_effect=self._set_selected_offset)

    async def _get_info(self):
        return self.info

    async def selected_offset(self) -> int:
        return self._selected

    async def selected_track(self):
        if not self.tracks:
            return None
        return self.tracks[self._selected]

    async def total_count(self) -> int:
        return len(self.tracks)

    async def available_count(self) -> int:
        return len(self.tracks)

    async def available_tracks(self):
        return list(self.tracks)

    async def _select_track_key(self, key: str) -> bool:
        rk = str(key).rstrip("/").rsplit("/", 1)[-1]
        for i, track in enumerate(self.tracks):
            if str(getattr(track, "ratingKey", "")) == rk:
                self._selected = i
                self.info.playQueueSelectedItemOffset = i
                return True
        return False

    async def _set_selected_offset(self, offset: int) -> None:
        self._selected = offset
        self.info.playQueueSelectedItemOffset = offset

    async def track_needs_transcode(self, track) -> bool:
        return False

    def _media_playability_values(self, media):
        return (None, None)


class FakePlexLib:
    def __init__(self, queues: dict[str, FakePlayQueue] | None = None) -> None:
        self.device = None
        self.client_identifier = None
        self._queues = queues or {}
        self.fetch_metadata = AsyncMock(side_effect=self._fetch_metadata)
        self.fetch_album_tracks = AsyncMock(side_effect=self._fetch_album_tracks)

    def update(self, query) -> None:
        return None

    def get_queue(self, container_key: str) -> FakePlayQueue:
        if container_key not in self._queues:
            self._queues[container_key] = FakePlayQueue(container_key=container_key)
        return self._queues[container_key]

    async def _fetch_metadata(self, key: str):
        rk = str(key).rstrip("/").rsplit("/", 1)[-1]
        return SimpleNamespace(
            type="album",
            title="Example Album",
            ratingKey=rk,
            key=key,
        )

    async def _fetch_album_tracks(self, album_key: str):
        return make_album_tracks(parent_key=str(album_key))
