"""playMedia UX: launch, duplicate retry, second album."""
from __future__ import annotations

import pytest

from tests.fakes.plex import make_album_tracks
from tests.scenarios.conftest import attach_queue


@pytest.mark.asyncio
async def test_single_track_sends_play_now(sm6_adapter):
    tracks = make_album_tracks(count=1)
    attach_queue(sm6_adapter, tracks=tracks, container_key="/playQueues/10")

    await sm6_adapter.play_media(
        "/playQueues/10",
        key=tracks[0].key,
        offset=0,
    )

    labels = sm6_adapter._fake_sm6.labels()
    assert any(l == "QueueFolder" for l in labels)
    actions = [e.action for e in sm6_adapter._fake_sm6.journal if e.label == "QueueFolder"]
    assert actions == ["PLAY_NOW"]
    assert sm6_adapter._fake_sm6.transport == "PLAYING"
    assert sm6_adapter.state.state == "PLAYING"


@pytest.mark.asyncio
async def test_duplicate_play_media_ignored_while_playing(sm6_adapter):
    tracks = make_album_tracks(count=1)
    attach_queue(sm6_adapter, tracks=tracks, container_key="/playQueues/11")

    await sm6_adapter.play_media("/playQueues/11", key=tracks[0].key, offset=0)
    first_count = sm6_adapter._fake_sm6.labels().count("QueueFolder")

    await sm6_adapter.play_media("/playQueues/11", key=tracks[0].key, offset=0)
    assert sm6_adapter._fake_sm6.labels().count("QueueFolder") == first_count
    assert len(sm6_adapter._fake_sm6.playlist) == 1


@pytest.mark.asyncio
async def test_new_play_queue_is_not_treated_as_duplicate(sm6_adapter):
    tracks_a = make_album_tracks(count=1, start_key=900001)
    tracks_b = make_album_tracks(count=1, start_key=900011, album="Other Album")
    attach_queue(sm6_adapter, tracks=tracks_a, container_key="/playQueues/20")

    await sm6_adapter.play_media("/playQueues/20", key=tracks_a[0].key, offset=0)
    attach_queue(sm6_adapter, tracks=tracks_b, container_key="/playQueues/21")
    await sm6_adapter.play_media("/playQueues/21", key=tracks_b[0].key, offset=0)

    assert sm6_adapter._fake_sm6.labels().count("QueueFolder") == 2
