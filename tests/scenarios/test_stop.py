"""STOP UX: playlist cleared, no APPEND after DeleteAll."""
from __future__ import annotations

import asyncio

import pytest

from tests.fakes.plex import make_album_tracks
from tests.scenarios.conftest import attach_queue


@pytest.mark.asyncio
async def test_plex_stop_clears_sm6_playlist(sm6_adapter):
    tracks = make_album_tracks(count=1)
    attach_queue(sm6_adapter, tracks=tracks, container_key="/playQueues/30")
    await sm6_adapter.play_media("/playQueues/30", key=tracks[0].key, offset=0)
    assert sm6_adapter._fake_sm6.playlist

    await sm6_adapter.stop()

    labels = sm6_adapter._fake_sm6.labels()
    assert "KeyPressed STOP" in labels
    assert "DeleteAll" in labels
    assert labels.index("KeyPressed STOP") < labels.index("DeleteAll")
    assert sm6_adapter._fake_sm6.playlist == []
    assert sm6_adapter.queue is None
    assert sm6_adapter._sm6_sonoplay_owned_playback is False


@pytest.mark.asyncio
async def test_stop_waits_for_in_flight_queue_folder_before_delete_all(sm6_adapter, fake_sm6):
    """APPEND still in flight must finish before DeleteAll (no refill after clear)."""
    from dlna.sm6_control import Sm6Control

    sm6 = Sm6Control(sm6_adapter.dlna.location_url)
    fake_sm6.hang_labels.add("QueueFolder")
    append = asyncio.create_task(
        sm6.queue_folder("<DIDL/>", action="APPEND", server_udn="uuid:plex", navigator_id="nav")
    )
    await asyncio.sleep(0.01)

    stop_task = asyncio.create_task(sm6_adapter.stop())
    await asyncio.sleep(0.02)
    assert "DeleteAll" not in fake_sm6.labels()

    fake_sm6.release_hang()
    await append
    await stop_task

    labels = fake_sm6.labels()
    assert labels.index("QueueFolder") < labels.index("DeleteAll")
    assert fake_sm6.playlist == []


@pytest.mark.asyncio
async def test_stop_ignored_during_playmedia_takeover(sm6_adapter):
    tracks = make_album_tracks(count=1)
    attach_queue(sm6_adapter, tracks=tracks, container_key="/playQueues/31")
    sm6_adapter._sm6_plex_play_in_progress = True

    await sm6_adapter.stop()

    assert "KeyPressed STOP" not in sm6_adapter._fake_sm6.labels()
    assert sm6_adapter.queue is not None
