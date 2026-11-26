"""playMedia UX: multi-album playQueue (REPLACE first track, then tail APPEND)."""
from __future__ import annotations

import asyncio

import pytest

from plex.adapters import PlexDlnaAdapter
from tests.fakes.plex import make_album_tracks
from tests.scenarios.conftest import attach_queue


@pytest.mark.asyncio
async def test_plex_queue_not_realigned_on_partial_sm6_playlist(sm6_adapter, monkeypatch):
    tracks = make_album_tracks(count=2, parent_key="900100") + make_album_tracks(
        count=2, start_key=900011, album="Other Album", parent_key="900200"
    )
    queue = attach_queue(sm6_adapter, tracks=tracks, container_key="/playQueues/30")

    monkeypatch.setattr(
        sm6_adapter,
        "_sm6_after_queue_folder",
        PlexDlnaAdapter._sm6_after_queue_folder.__get__(sm6_adapter),
    )
    realign_at: list[int] = []

    async def _realign():
        realign_at.append(len(sm6_adapter._fake_sm6.playlist))

    monkeypatch.setattr(sm6_adapter, "_sm6_realign_plex_queue_with_sm6", _realign)

    await sm6_adapter.play_media("/playQueues/30", key=tracks[0].key, offset=0)
    await asyncio.wait_for(sm6_adapter._sm6_tail_fill_task, timeout=5)

    actions = [e.action for e in sm6_adapter._fake_sm6.journal if e.label == "QueueFolder"]
    assert actions[0] in {"REPLACE", "PLAY_NOW"}
    assert actions[1:] == ["APPEND"] * 3
    # Realign only once the SM6 holds the whole queue; Plex queue untouched.
    assert realign_at == [4]
    assert sm6_adapter.queue is queue


@pytest.mark.asyncio
async def test_rebuild_refused_while_tail_fill_builds(sm6_adapter):
    tracks = make_album_tracks(count=1)
    attach_queue(sm6_adapter, tracks=tracks)
    blocker = asyncio.Event()
    sm6_adapter._sm6_tail_fill_task = asyncio.create_task(blocker.wait())
    try:
        assert await sm6_adapter._sm6_rebuild_plex_playlist_from_sm6(force=True) is False
        sm6_adapter._ensure_plex_lib_for_sm6_api.assert_not_called()
    finally:
        blocker.set()
        await sm6_adapter._sm6_tail_fill_task
