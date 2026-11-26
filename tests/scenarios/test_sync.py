"""Passive sync / detach UX."""
from __future__ import annotations

import pytest

from plex.sm6_session_rules import sm6_should_detach_on_audio_source
from tests.fakes.plex import make_album_tracks
from tests.scenarios.conftest import attach_queue


def test_other_audio_source_should_detach():
    assert sm6_should_detach_on_audio_source(
        10, 2, media_player_id=10, sonoplay_owned=True
    ) is True
    assert sm6_should_detach_on_audio_source(
        10, 10, media_player_id=10, sonoplay_owned=True
    ) is False


@pytest.mark.asyncio
async def test_notify_stopped_while_owned_keeps_playing(sm6_adapter):
    """Stale STOPPED notify must not tear down an owned Plex session."""
    from types import SimpleNamespace

    tracks = make_album_tracks(count=1)
    attach_queue(sm6_adapter, tracks=tracks, container_key="/playQueues/50")
    await sm6_adapter.play_media("/playQueues/50", key=tracks[0].key, offset=0)
    assert sm6_adapter.state.state == "PLAYING"
    sm6_adapter._sm6_sonoplay_owned_playback = True

    instance = SimpleNamespace(
        TransportState={"@val": "STOPPED"},
        AVTransportURI={"@val": ""},
        RelativeTimePosition={"@val": ""},
    )
    info = SimpleNamespace(
        propertyset=SimpleNamespace(
            property=SimpleNamespace(
                LastChange=SimpleNamespace(Event=SimpleNamespace(InstanceID=instance))
            )
        )
    )
    sm6_adapter.update_state(info)
    assert sm6_adapter.state.state == "PLAYING"
