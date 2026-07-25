"""Detach must surface disconnected="1" on timeline poll, not only push notify."""

import asyncio
import importlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import (
    drop_stub_modules,
    ensure_real_dlna_package,
    ensure_real_plex_package,
    reload_module,
)

drop_stub_modules()
ensure_real_plex_package()
# Force a clean dlna package (empty shell modules report "unknown location").
for _key in list(sys.modules):
    if _key == "dlna" or _key.startswith("dlna."):
        del sys.modules[_key]
ensure_real_dlna_package()
subscribe = reload_module("plex.subscribe")
SubscribeManager = subscribe.SubscribeManager
Subscriber = subscribe.Subscriber
TIMELINE_DISCONNECTED = subscribe.TIMELINE_DISCONNECTED
TIMELINE_STOPPED = subscribe.TIMELINE_STOPPED


@pytest.mark.asyncio
async def test_msg_for_device_returns_disconnected_when_sm6_detached() -> None:
    device = MagicMock()
    adapter = MagicMock()
    adapter.no_notice = False
    adapter._sm6_plex_clients_detached = True
    adapter.state.state = "STOPPED"
    adapter.queue = None

    with patch("plex.subscribe.adapter_by_device", new=AsyncMock(return_value=adapter)):
        msg = await SubscribeManager().msg_for_device(device)

    assert msg == TIMELINE_DISCONNECTED
    assert 'disconnected="1"' in msg


@pytest.mark.asyncio
async def test_timeline_poll_returns_disconnected_without_wait_when_detached() -> None:
    """Detached latch must answer poll immediately (no wait=1 / PMS notify)."""
    ensure_real_plex_package()
    plexserver = reload_module("plex.plexserver")

    device = MagicMock()
    device.uuid = "device-uuid"
    del device.loop_subscribe
    adapter = MagicMock()
    adapter._sm6_plex_clients_detached = True
    adapter._is_sm6_renderer = MagicMock(return_value=True)
    adapter._sm6_should_skip_volume_poll = AsyncMock()
    adapter._sm6_sync_timeline_for_poll = AsyncMock()
    adapter.wait_for_event = AsyncMock()

    request = MagicMock()
    request.query_params = {}
    request.url = "http://test/player/timeline/poll"

    with (
        patch.object(plexserver, "guess_host_ip", new=AsyncMock()),
        patch.object(plexserver, "get_device_by_uuid", new=AsyncMock(return_value=device)),
        patch.object(plexserver, "adapter_by_device", new=AsyncMock(return_value=adapter)),
        patch.object(plexserver.sub_man, "update_command_id"),
        patch.object(plexserver.sub_man, "msg_for_device", new=AsyncMock()) as msg_for,
        patch.object(plexserver.sub_man, "notify_server_device", new=AsyncMock()) as notify_pms,
        patch.object(plexserver, "build_response", new=AsyncMock(return_value="ok")) as build,
        patch("plex.plex_client.note_client_product"),
    ):
        result = await plexserver.timeline_poll(
            request,
            commandID=7,
            wait=1,
            target_uuid="device-uuid",
            client_uuid="client-uuid",
            plex_product="Plexamp",
        )

    assert result == "ok"
    build.assert_awaited_once()
    assert 'disconnected="1"' in build.await_args.args[0]
    adapter.wait_for_event.assert_not_awaited()
    adapter._sm6_sync_timeline_for_poll.assert_not_awaited()
    adapter._sm6_should_skip_volume_poll.assert_not_awaited()
    msg_for.assert_not_awaited()
    notify_pms.assert_not_called()


@pytest.mark.asyncio
async def test_msg_for_device_stopped_when_not_detached() -> None:
    device = MagicMock()
    adapter = MagicMock()
    adapter.no_notice = False
    adapter._sm6_plex_clients_detached = False
    adapter.state.state = "STOPPED"
    adapter.queue = None

    with patch("plex.subscribe.adapter_by_device", new=AsyncMock(return_value=adapter)):
        msg = await SubscribeManager().msg_for_device(device)

    assert msg == TIMELINE_STOPPED
    assert "disconnected" not in msg


def test_note_polled_audio_source_latches_detach_for_poll() -> None:
    from plex.adapters import PlexDlnaAdapter

    adapter = MagicMock(spec=PlexDlnaAdapter)
    adapter._sm6_relinquished_control = False
    adapter._sm6_plex_clients_detached = False
    adapter._sm6_last_audio_source = 10
    adapter._sm6_sonoplay_owned_playback = True
    adapter._sm6_plex_play_in_progress = True
    adapter.wait_state_change_events = []
    adapter.dlna = MagicMock(name="SM6")
    waiter = {"event": MagicMock()}
    adapter.wait_state_change_events = [waiter]
    adapter._sm6_abort_queue_work = MagicMock()
    adapter._sm6_clear_optimistic_play = MagicMock()

    result = PlexDlnaAdapter._sm6_note_polled_audio_source(adapter, 2)

    assert result == 2
    assert adapter._sm6_plex_clients_detached is True
    assert adapter._sm6_relinquished_control is True
    adapter._sm6_abort_queue_work.assert_called_once()
    waiter["event"].set.assert_called_once()


def test_note_polled_detaches_after_device_stopped_relinquish() -> None:
    """device_stopped sets relinquished before source poll — must still latch detach."""
    from plex.adapters import PlexDlnaAdapter

    adapter = MagicMock(spec=PlexDlnaAdapter)
    adapter._sm6_relinquished_control = True
    adapter._sm6_plex_clients_detached = False
    adapter._sm6_last_audio_source = 10
    adapter._sm6_sonoplay_owned_playback = False
    adapter._sm6_plex_play_in_progress = False
    adapter.wait_state_change_events = []
    adapter.dlna = MagicMock(name="SM6")
    adapter._sm6_abort_queue_work = MagicMock()
    adapter._sm6_clear_optimistic_play = MagicMock()

    result = PlexDlnaAdapter._sm6_note_polled_audio_source(adapter, 2)

    assert result == 2
    assert adapter._sm6_plex_clients_detached is True
    adapter._sm6_abort_queue_work.assert_called_once()


def test_note_polled_skips_when_already_detached() -> None:
    from plex.adapters import PlexDlnaAdapter

    adapter = MagicMock(spec=PlexDlnaAdapter)
    adapter._sm6_plex_clients_detached = True
    adapter._sm6_last_audio_source = 2
    adapter.wait_state_change_events = []
    adapter.dlna = MagicMock(name="SM6")
    adapter._sm6_dispatcher = MagicMock()

    assert PlexDlnaAdapter._sm6_note_polled_audio_source(adapter, 3) is None
    adapter._sm6_dispatcher.assert_not_called()
    assert adapter._sm6_last_audio_source == 3
    assert adapter._sm6_plex_clients_detached is True


def test_note_polled_clears_detach_when_media_player_again() -> None:
    """After 10→other latch, returning to Media Player must re-arm Plex attach."""
    from plex.adapters import PlexDlnaAdapter

    adapter = MagicMock(spec=PlexDlnaAdapter)
    adapter._sm6_plex_clients_detached = True
    adapter._sm6_last_audio_source = 2
    adapter.wait_state_change_events = []
    adapter.dlna = MagicMock()
    adapter.dlna.name = "SM6"
    waiter = {"event": MagicMock()}
    adapter.wait_state_change_events = [waiter]

    assert PlexDlnaAdapter._sm6_note_polled_audio_source(adapter, 10) is None
    assert adapter._sm6_plex_clients_detached is False
    assert adapter._sm6_last_audio_source == 10
    waiter["event"].set.assert_called_once()


@pytest.mark.asyncio
async def test_detach_forces_stopped_and_pms_notify() -> None:
    from plex.adapters import PlexDlnaAdapter

    adapter = PlexDlnaAdapter.__new__(PlexDlnaAdapter)
    adapter.dlna = MagicMock()
    adapter.dlna.name = "SM6"
    adapter.state = MagicMock()
    adapter._sm6_session_uri = "http://track"
    adapter.wait_state_change_events = []

    with patch("plex.subscribe.sub_man") as sub_man:
        sub_man.notify_device_disconnected = AsyncMock()
        sub_man.notify_server_device = AsyncMock()
        await adapter._sm6_detach_plex_for_external_source(2)
        adapter.state.update.assert_called_with(state="STOPPED", uri=None)
        sub_man.notify_device_disconnected.assert_awaited_once_with(adapter.dlna)
        sub_man.notify_server_device.assert_awaited_once_with(adapter.dlna, force=True)
    assert adapter._sm6_session_uri is None


@pytest.mark.asyncio
async def test_detach_for_external_source_keeps_plex_bind_token() -> None:
    """Local fallback must not revoke plex.tv account link (unlike /api/plex-disconnect)."""
    from plex.adapters import PlexDlnaAdapter

    adapter = PlexDlnaAdapter.__new__(PlexDlnaAdapter)
    adapter.dlna = MagicMock()
    adapter.dlna.name = "SM6"
    adapter.dlna.uuid = "device-uuid"
    adapter.plex_bind_token = "keep-me"
    adapter.state = MagicMock()
    adapter._sm6_session_uri = None
    adapter.wait_state_change_events = []

    with patch("plex.subscribe.sub_man") as sub_man:
        sub_man.notify_device_disconnected = AsyncMock()
        sub_man.notify_server_device = AsyncMock()
        await adapter._sm6_detach_plex_for_external_source(2)
        sub_man.notify_device_disconnected.assert_awaited_once_with(adapter.dlna)

    assert adapter.plex_bind_token == "keep-me"
    assert not hasattr(PlexDlnaAdapter, "_sm6_unlink_plex_device")


@pytest.mark.asyncio
async def test_notify_device_disconnected_removes_subs_before_slow_push() -> None:
    """Detach must drop registrations immediately, not after push send RTT."""
    device = MagicMock()
    device.uuid = "device-uuid"
    manager = SubscribeManager()
    sub = Subscriber("client-uuid", "127.0.0.1", 32400, manager)
    manager.subscribers[device.uuid] = [sub]

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_send(msg, dev):
        started.set()
        await release.wait()

    sub.send = _slow_send  # type: ignore[method-assign]

    task = asyncio.create_task(manager.notify_device_disconnected(device))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert manager.subscribers.get(device.uuid, []) == []
    release.set()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_sm6_on_plex_client_subscribe_clears_latch_without_reclaim() -> None:
    """Subscribe re-arms attach but must not force source 10."""
    from plex.adapters import PlexDlnaAdapter

    adapter = PlexDlnaAdapter.__new__(PlexDlnaAdapter)
    adapter.dlna = MagicMock()
    adapter.dlna.name = "SM6"
    adapter.dlna.location_url = "http://sm6/"
    adapter._is_sm6_renderer = lambda: True
    adapter._sm6_plex_clients_detached = True
    adapter._sm6_last_audio_source = 2
    adapter._sm6_relinquished_control = True
    adapter._suppress_auto_next = True
    adapter.wait_state_change_events = []
    adapter._sm6_run_control = AsyncMock()

    with patch("dlna.sm6_control.Sm6Control") as control_cls:
        await adapter._sm6_on_plex_client_subscribe()
        control_cls.assert_not_called()

    adapter._sm6_run_control.assert_not_awaited()
    assert adapter._sm6_plex_clients_detached is False
    assert adapter._sm6_relinquished_control is True
    assert adapter._sm6_last_audio_source == 2
    assert adapter._suppress_auto_next is True


@pytest.mark.asyncio
async def test_sm6_on_plex_client_unsubscribe_clears_latch() -> None:
    """Unsubscribe after local fallback must re-arm the next select."""
    from plex.adapters import PlexDlnaAdapter

    adapter = PlexDlnaAdapter.__new__(PlexDlnaAdapter)
    adapter.dlna = MagicMock()
    adapter.dlna.name = "SM6"
    adapter._is_sm6_renderer = lambda: True
    adapter._sm6_plex_clients_detached = True
    adapter.wait_state_change_events = []

    await adapter._sm6_on_plex_client_unsubscribe()
    assert adapter._sm6_plex_clients_detached is False


@pytest.mark.asyncio
async def test_sm6_on_plex_client_subscribe_noop_when_not_detached() -> None:
    from plex.adapters import PlexDlnaAdapter

    adapter = PlexDlnaAdapter.__new__(PlexDlnaAdapter)
    adapter.dlna = MagicMock()
    adapter.dlna.name = "SM6"
    adapter._is_sm6_renderer = lambda: True
    adapter._sm6_plex_clients_detached = False
    adapter._sm6_last_audio_source = 10
    adapter._sm6_relinquished_control = False
    adapter._suppress_auto_next = False
    adapter.wait_state_change_events = []
    adapter._sm6_run_control = AsyncMock()

    await adapter._sm6_on_plex_client_subscribe()
    adapter._sm6_run_control.assert_not_awaited()
    assert adapter._sm6_plex_clients_detached is False


def test_relinquish_plex_stop_aborts_tail_fill() -> None:
    """plex_stop must cancel QueueFolder APPEND still in flight (log 11:16:46–53)."""
    from plex.adapters import PlexDlnaAdapter

    adapter = PlexDlnaAdapter.__new__(PlexDlnaAdapter)
    adapter.dlna = MagicMock()
    adapter.dlna.name = "SM6"
    adapter._sm6_relinquished_control = False
    adapter._sm6_sonoplay_owned_playback = True
    adapter._sm6_outbound_until = None
    adapter._suppress_auto_next = False
    adapter._last_operation_finish_time = None
    adapter.wait_state_change_events = []
    adapter.state = MagicMock()
    adapter._sm6_clear_optimistic_play = MagicMock()
    adapter._sm6_schedule_plex_neutral = MagicMock()
    adapter._sm6_wake_waiters = MagicMock()
    adapter._sm6_tail_fill_task = MagicMock()
    adapter._sm6_tail_fill_task.done.return_value = False
    tail_task = adapter._sm6_tail_fill_task
    dispatcher = MagicMock()
    adapter._sm6_dispatcher = MagicMock(return_value=dispatcher)

    adapter._sm6_relinquish_control("plex_stop")

    tail_task.cancel.assert_called_once()
    assert adapter._sm6_tail_fill_task is None
    dispatcher.bump_generation.assert_called_once()
    adapter._sm6_schedule_plex_neutral.assert_called_once_with("plex_stop")


@pytest.mark.asyncio
async def test_apply_queue_track_noop_when_detached() -> None:
    """In-flight playlist sync must not mirror to Plex after detach latch."""
    from plex.adapters import PlexDlnaAdapter

    adapter = PlexDlnaAdapter.__new__(PlexDlnaAdapter)
    adapter.dlna = MagicMock()
    adapter.dlna.name = "SM6"
    adapter._sm6_plex_clients_detached = True
    adapter._sm6_entry_for_track_id = MagicMock(return_value=MagicMock())
    adapter._sm6_polled_track_uri = AsyncMock()
    adapter._sm6_resolve_entry_to_track = AsyncMock()

    assert await adapter._sm6_apply_queue_track(1, queue_index=0) is False
    adapter._sm6_polled_track_uri.assert_not_awaited()
    adapter._sm6_resolve_entry_to_track.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_sync_playlist_aborts_if_detached_after_position_poll() -> None:
    """Detach during get_current_queue_position must not call apply_queue_track."""
    from plex.adapters import PlexDlnaAdapter

    adapter = PlexDlnaAdapter.__new__(PlexDlnaAdapter)
    adapter.dlna = MagicMock()
    adapter.dlna.name = "SM6"
    adapter.dlna.location_url = "http://sm6/"
    adapter._is_sm6_renderer = lambda: True
    adapter._sm6_plex_clients_detached = False
    adapter._sm6_plex_play_in_progress = False
    adapter._sm6_relinquished_control = False
    adapter._sm6_sonoplay_owned_playback = True
    adapter._sm6_last_queue_track_id = None
    adapter._sm6_queue_base_offset = 0
    adapter.state = MagicMock()
    adapter.state.state = "PLAYING"
    adapter.queue = None
    adapter._sm6_apply_queue_track = AsyncMock(return_value=True)

    async def _position_then_detach():
        adapter._sm6_plex_clients_detached = True
        return (42, 0)

    with patch("dlna.sm6_control.Sm6Control") as control_cls:
        control_cls.return_value.get_current_queue_position = AsyncMock(
            side_effect=_position_then_detach
        )
        await adapter._sm6_maybe_sync_playlist()

    adapter._sm6_apply_queue_track.assert_not_awaited()
