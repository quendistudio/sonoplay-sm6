"""Transport UX: pause / skip via KeyPressed."""
from __future__ import annotations

import pytest

from tests.fakes.plex import make_album_tracks
from tests.scenarios.conftest import attach_queue


@pytest.mark.asyncio
async def test_pause_sends_play_pause_key(sm6_adapter):
    tracks = make_album_tracks(count=1)
    attach_queue(sm6_adapter, tracks=tracks, container_key="/playQueues/40")
    await sm6_adapter.play_media("/playQueues/40", key=tracks[0].key, offset=0)

    await sm6_adapter.pause()

    assert "KeyPressed PLAY_PAUSE" in sm6_adapter._fake_sm6.labels()
    assert sm6_adapter._fake_sm6.transport == "PAUSED_PLAYBACK"


@pytest.mark.asyncio
async def test_next_sends_skip_next(sm6_adapter):
    tracks = make_album_tracks(count=1)
    attach_queue(sm6_adapter, tracks=tracks, container_key="/playQueues/41")
    await sm6_adapter.play_media("/playQueues/41", key=tracks[0].key, offset=0)
    sm6_adapter._sm6_last_play_mode = "playlist"

    await sm6_adapter.next()

    assert "KeyPressed SKIP_NEXT" in sm6_adapter._fake_sm6.labels()


@pytest.mark.asyncio
async def test_prev_sends_skip_previous(sm6_adapter):
    tracks = make_album_tracks(count=1)
    attach_queue(sm6_adapter, tracks=tracks, container_key="/playQueues/42")
    await sm6_adapter.play_media("/playQueues/42", key=tracks[0].key, offset=0)
    sm6_adapter._sm6_last_play_mode = "playlist"

    await sm6_adapter.prev()

    assert "KeyPressed SKIP_PREVIOUS" in sm6_adapter._fake_sm6.labels()


@pytest.mark.asyncio
async def test_sm6_unavailable_on_pause_raises_clean_error(sm6_adapter, fake_sm6):
    tracks = make_album_tracks(count=1)
    attach_queue(sm6_adapter, tracks=tracks, container_key="/playQueues/43")
    await sm6_adapter.play_media("/playQueues/43", key=tracks[0].key, offset=0)

    fake_sm6.refuse_labels.add("KeyPressed PLAY_PAUSE")
    fake_sm6.refuse_labels.add("KeyPressed")

    with pytest.raises(RuntimeError, match="unavailable"):
        await sm6_adapter.pause()
