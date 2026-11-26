"""Fixtures for SM6 adapter scenarios."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.fakes.plex import FakePlayQueue, FakePlexLib, make_album_tracks
from tests.fakes.sm6 import FakeSm6, fake_dlna_device


@pytest.fixture
async def fake_sm6(monkeypatch):
    sm6 = FakeSm6()
    sm6.install(monkeypatch)
    return sm6


@pytest.fixture
async def sm6_adapter(fake_sm6, monkeypatch, temp_data_dir, settings_override):
    from plex.adapters import PlexDlnaAdapter
    from plex import url_resolver as url_resolver_mod

    settings_override(
        host_ip="192.168.50.99",
        plex_dlna_port=32469,
        plex_dlna_device_url="http://192.168.50.99:32469/DeviceDescription.xml",
    )

    # Avoid real Plex DLNA lookups during QueueFolder prep.
    monkeypatch.setattr(
        url_resolver_mod,
        "get_url_resolver",
        lambda: MagicMock(name="url_resolver"),
    )

    # DlnaState.__init__ starts a SOAP poll thread — disable it for scenarios.
    monkeypatch.setattr(
        "plex.adapters.DlnaState.start_looping",
        lambda self: None,
    )

    device = fake_dlna_device()
    adapter = PlexDlnaAdapter(device)

    plex = FakePlexLib()
    adapter.plex_lib = plex

    async def _ready(*, plex_takeover: bool = False):
        return None

    async def _resolve_track(track, resolver, *, paused=False, playlist_item=False):
        url = f"http://192.168.50.99:32469/object/{getattr(track, 'ratingKey', 'x')}/file.flac"
        didl = f"<DIDL-Lite><item id='{getattr(track, 'ratingKey', 'x')}'/></DIDL-Lite>"
        return didl, url, None, 0

    async def _server_udn():
        return "uuid:3bc6d600-ec61-c760-ca28-d377e33cb8d8"

    async def _refresh(*, fetch_tracks: bool = True):
        return None

    async def _after(*, offset: int = 0, paused: bool = False):
        return None

    monkeypatch.setattr(adapter, "_ensure_sm6_ready", _ready)
    monkeypatch.setattr(adapter, "_sm6_resolve_track_playback", _resolve_track)
    monkeypatch.setattr(adapter, "_sm6_plex_server_udn", _server_udn)
    monkeypatch.setattr(adapter, "_sm6_refresh_playlist_snapshot", _refresh)
    monkeypatch.setattr(adapter, "_sm6_after_queue_folder", _after)
    monkeypatch.setattr(adapter, "_sm6_notify_plex_timeline_sync", lambda: None)
    monkeypatch.setattr(adapter, "_sm6_wake_waiters", lambda: None)
    # Never hit a real PMS when clearing the playQueue mirror.
    monkeypatch.setattr(
        adapter, "_ensure_plex_lib_for_sm6_api", AsyncMock(return_value=False)
    )

    adapter._fake_sm6 = fake_sm6
    adapter._fake_plex = plex
    yield adapter

    adapter.state.shutdown()
    try:
        from dlna.sm6_dispatcher import _DISPATCHERS

        for dispatcher in list(_DISPATCHERS.values()):
            dispatcher._shutdown = True
            task = getattr(dispatcher, "_executor_task", None)
            if task is not None and not task.done():
                task.cancel()
            for lane_task in (
                getattr(dispatcher, "_active_queue_task", None),
                getattr(dispatcher, "_active_transport_task", None),
            ):
                if lane_task is not None and not lane_task.done():
                    lane_task.cancel()
        _DISPATCHERS.clear()
    except Exception:
        pass


def attach_queue(adapter, *, tracks=None, container_key="/playQueues/1", selected=0):
    queue = FakePlayQueue(
        tracks or make_album_tracks(),
        container_key=container_key,
        selected=selected,
    )
    adapter._fake_plex._queues[container_key] = queue
    adapter.queue = queue
    return queue
