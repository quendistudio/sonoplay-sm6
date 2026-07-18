"""Tests SM6 → Plex playQueue offset sync during SonoPlay-owned playback."""

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Minimal stubs — do not stub plex.url_resolver / plex.sm6_sync (other unit tests import them).
_STUB_MODULES = [
    "dotmap", "aiohttp", "aiohttp.ClientConnectionError",
    "xmltodict", "uvicorn",
    "starlette", "starlette.datastructures",
    "fastapi", "fastapi.responses", "fastapi.templating",
    "fastapi.staticfiles",
    "pydantic", "pydantic.settings",
    "jinja2",
    "utils", "settings", "version",
    "plex.play_queue", "plex.subscribe", "plex.gdm", "plex.pin_login",
    "plex.plexserver",
    "dlna", "dlna.dlna_device", "dlna.discover",
    "dlna.virtual", "dlna.virtual.devices", "dlna.virtual.volume",
    "dlna.quirks",
]


def _make_stub(name):
    mod = types.ModuleType(name)
    mod.__path__ = []
    mod.__file__ = f"<stub {name}>"
    mod.__getattr__ = lambda attr: MagicMock()
    return mod


for _name in _STUB_MODULES:
    if _name not in sys.modules:
        sys.modules[_name] = _make_stub(_name)

if "plex" not in sys.modules:
    _plex_pkg = types.ModuleType("plex")
    _plex_pkg.__path__ = ["plex"]
    _plex_pkg.__file__ = "<stub plex>"
    sys.modules["plex"] = _plex_pkg

sys.modules["settings"].settings = MagicMock()
_utils = sys.modules["utils"]
_utils.parse_timedelta = MagicMock(return_value=0)
_utils.convert_volume = MagicMock(return_value=50)
_utils.g = MagicMock()
_utils.pms_header = MagicMock(return_value={})
_utils.extract_value = MagicMock()
sys.modules["dotmap"].DotMap = MagicMock
sys.modules["starlette.datastructures"].QueryParams = MagicMock
sys.modules["plex.play_queue"].PlayQueue = MagicMock

from plex.adapters import PlexDlnaAdapter  # noqa: E402


def _sm6_adapter(**overrides):
    adapter = object.__new__(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6-test", location_url="http://sm6/")
    adapter.state = MagicMock(state="PLAYING")
    adapter.loop = None
    adapter.no_notice = True
    adapter.wait_state_change_events = []
    adapter.queue = AsyncMock()
    adapter.queue.selected_offset = AsyncMock(return_value=0)
    adapter.queue.set_selected_offset = AsyncMock()
    adapter.queue.selected_track = AsyncMock(return_value=MagicMock(
        ratingKey="200", playQueueItemID=42, title="Track B",
    ))
    adapter._sm6_on_sm6_track_advanced = AsyncMock()
    adapter.current_track_info = MagicMock(ratingKey="100")
    adapter._sm6_last_queue_track_id = 0
    adapter._sm6_queue_base_offset = 0
    adapter._sm6_session_uri = "http://track-a"
    adapter._sm6_sonoplay_owned_playback = True
    adapter._sm6_plex_play_in_progress = False
    adapter._sm6_relinquished_control = False
    adapter._sm6_playlist_snapshot = None
    adapter._sm6_on_media_player_source = MagicMock(return_value=True)
    adapter._sm6_any_poll_paused_sync = MagicMock(return_value=False)
    adapter._is_sm6_renderer = MagicMock(return_value=True)
    adapter._sm6_publish_track_change = MagicMock()
    adapter._sm6_entry_for_track_id = MagicMock(return_value=None)
    adapter._sm6_resolve_entry_to_track = AsyncMock()
    adapter._sm6_rebuild_plex_playlist_from_sm6 = AsyncMock(return_value=False)
    adapter._sm6_apply_queue_track = AsyncMock()
    for key, value in overrides.items():
        setattr(adapter, key, value)
    return adapter


@pytest.mark.asyncio
async def test_apply_queue_track_updates_plex_offset_on_track_change() -> None:
    adapter = _sm6_adapter()
    entry = MagicMock(title="Track B", artist="Artist")
    track = MagicMock(ratingKey="200", title="Track B")
    adapter._sm6_resolve_entry_to_track = AsyncMock(return_value=track)

    resolver = MagicMock(resolve_stream_url=AsyncMock(return_value="http://track-b"))
    with patch("plex.url_resolver.get_url_resolver", return_value=resolver):
        changed = await PlexDlnaAdapter._sm6_apply_queue_track(
            adapter,
            1,
            entry=entry,
            queue_index=1,
        )

    assert changed is True
    adapter.queue.set_selected_offset.assert_awaited_once_with(1)
    adapter.queue.selected_track.assert_awaited()
    adapter._sm6_on_sm6_track_advanced.assert_awaited_once()
    adapter._sm6_publish_track_change.assert_called_once()
    assert adapter.current_track_info.ratingKey == "200"
    assert adapter.current_track_info.playQueueItemID == 42


@pytest.mark.asyncio
async def test_maybe_sync_playlist_runs_while_sonoplay_owned() -> None:
    adapter = _sm6_adapter()
    sm6 = MagicMock()
    sm6.get_current_queue_position = AsyncMock(return_value=(2, 2))

    sm6_control = types.ModuleType("dlna.sm6_control")
    sm6_control.Sm6Control = MagicMock(return_value=sm6)
    with patch.dict(sys.modules, {"dlna.sm6_control": sm6_control}):
        await PlexDlnaAdapter._sm6_maybe_sync_playlist(adapter, force=False)

    adapter._sm6_apply_queue_track.assert_awaited_once_with(2, queue_index=2)


@pytest.mark.asyncio
async def test_sync_from_polled_uri_keeps_play_queue_item_id() -> None:
    adapter = _sm6_adapter()
    adapter._ensure_plex_lib_for_sm6_api = AsyncMock(return_value=True)
    adapter._sm6_rating_key_for_uri = AsyncMock(return_value="200")
    adapter.current_track_info = MagicMock(ratingKey="100", playQueueItemID=10)
    adapter._sm6_session_uri = "http://old"
    queue_track = MagicMock(ratingKey="200", playQueueItemID=99, title="Track B")
    adapter.queue.select_track_key = AsyncMock(return_value=True)
    adapter.queue.selected_track = AsyncMock(return_value=queue_track)

    uri = "http://example:32488/player/stream/transcode.mp3?ratingKey=200&device=x"
    await PlexDlnaAdapter._sm6_sync_from_polled_uri(adapter, uri)

    assert adapter.current_track_info is queue_track
    assert adapter._sm6_session_uri == uri
