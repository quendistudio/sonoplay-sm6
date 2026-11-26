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


@pytest.mark.asyncio
async def test_playlist_resolve_keeps_each_entry_own_track(sm6_adapter, monkeypatch):
    """Current track URI must not be stamped on every SM6 playlist entry."""
    from dlna.sm6_playlist import Sm6PlaylistEntry, Sm6PlaylistState

    tracks = make_album_tracks(count=3)
    by_key = {t.ratingKey: t for t in tracks}

    async def _fetch(key):
        return by_key.get(str(key).rsplit("/", 1)[-1])

    sm6_adapter._fake_plex.fetch_metadata.side_effect = _fetch
    current_uri = f"http://192.168.50.99:32469/object/x/file.flac?ratingKey={tracks[0].ratingKey}"
    sm6_adapter._sm6_session_uri = current_uri

    async def _polled():
        return current_uri

    monkeypatch.setattr(sm6_adapter, "_sm6_polled_track_uri", _polled)
    sm6_adapter._sm6_track_rating_keys = {
        11: tracks[1].ratingKey,
        12: tracks[2].ratingKey,
    }
    state = Sm6PlaylistState(
        length=3,
        current_track_id=10,
        media_queue_index=0,
        tracks=tuple(
            Sm6PlaylistEntry(track_id=10 + i, title=t.title, artist="Example Artist")
            for i, t in enumerate(tracks)
        ),
    )

    keys, _tracks, selected = await sm6_adapter._sm6_resolve_playlist_to_rating_keys(state)

    assert keys == [t.ratingKey for t in tracks]
    assert selected == 0
