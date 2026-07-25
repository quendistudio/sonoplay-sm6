"""Behavior test: SM6 track advance updates Plex playQueue cursor."""

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import bare_sm6_adapter, drop_stub_modules, purge_settings_modules

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

# Do not leave import stubs for other test modules that need real packages.
drop_stub_modules()
purge_settings_modules()


@pytest.mark.asyncio
async def test_sm6_queue_index_moves_plex_playqueue_cursor() -> None:
    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6-test")
    adapter.queue = AsyncMock()
    adapter.queue.selected_offset = AsyncMock(return_value=0)
    adapter.queue.set_selected_offset = AsyncMock()
    adapter.queue.select_track_key = AsyncMock(return_value=True)
    adapter.queue.selected_track = AsyncMock(
        return_value=MagicMock(ratingKey="100", playQueueItemID=42, title="Track B"),
    )
    adapter.current_track_info = MagicMock(ratingKey="100")
    adapter._sm6_queue_base_offset = 5

    changed = await PlexDlnaAdapter._sm6_sync_plex_queue_offset(adapter, 3)

    adapter.queue.select_track_key.assert_not_awaited()
    assert adapter.current_track_info.ratingKey == "100"
    assert adapter.current_track_info.playQueueItemID == 42
    # Digit ratingKey path binds via selected_track; offset move is separate.
    adapter.queue.set_selected_offset.assert_not_awaited()
    assert changed is True  # plex offset 5+3=8 vs selected 0


@pytest.mark.asyncio
async def test_sm6_queue_sync_selects_by_rating_key_when_offset_wrong() -> None:
    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6-test")
    adapter.queue = AsyncMock()
    adapter.queue.selected_offset = AsyncMock(return_value=0)
    adapter.queue.set_selected_offset = AsyncMock()
    adapter.queue.select_track_key = AsyncMock(return_value=True)
    wrong_track = MagicMock(ratingKey="200", playQueueItemID=99, title="Wrong")
    correct_track = MagicMock(ratingKey="100", playQueueItemID=42, title="First Light")
    adapter.queue.selected_track = AsyncMock(side_effect=[wrong_track, correct_track])
    adapter.current_track_info = MagicMock(ratingKey="100")
    adapter._sm6_queue_base_offset = 5

    await PlexDlnaAdapter._sm6_sync_plex_queue_offset(adapter, 3)

    adapter.queue.select_track_key.assert_awaited_once_with("/library/metadata/100")
    assert adapter.current_track_info.ratingKey == "100"
    assert adapter.current_track_info.playQueueItemID == 42


@pytest.mark.asyncio
async def test_sm6_prev_uses_skip_key_for_playlist_mode(monkeypatch) -> None:
    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6-test")
    adapter.queue = MagicMock()
    adapter._is_sm6_renderer = lambda: True
    adapter._detect_sm6_play_mode = AsyncMock(return_value="playlist")
    skip_mock = AsyncMock()
    monkeypatch.setattr(PlexDlnaAdapter, "_sm6_skip_via_key", skip_mock)

    await PlexDlnaAdapter.prev(adapter)

    skip_mock.assert_awaited_once_with(previous=True)


@pytest.mark.asyncio
async def test_sm6_prev_double_tap_bumps_queue(monkeypatch) -> None:
    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6-test")
    adapter.state = MagicMock()
    adapter.queue = MagicMock()
    adapter.current_track_info = MagicMock(title="Prev", duration=180000)
    adapter._is_sm6_renderer = lambda: True
    adapter._sm6_last_skip_previous_mono = 100.0
    adapter._sm6_send_key = AsyncMock()
    adapter._sm6_bump_queue_offset = AsyncMock()
    adapter._sm6_publish_track_change = MagicMock()
    adapter._sm6_enter_playing = MagicMock()
    adapter._sm6_sync_after_skip = AsyncMock()
    adapter._sm6_clear_optimistic_play = MagicMock()
    adapter._sm6_begin_optimistic_play = MagicMock()
    monkeypatch.setattr(sys.modules["plex.adapters"].time, "monotonic", lambda: 101.0)
    monkeypatch.setattr(
        sys.modules["plex.adapters"].asyncio,
        "create_task",
        lambda coro: (coro.close() if hasattr(coro, "close") else None) or MagicMock(),
    )
    monkeypatch.setitem(
        sys.modules,
        "dlna.sm6_simple_remote",
        types.SimpleNamespace(KEY_SKIP_NEXT="NEXT", KEY_SKIP_PREVIOUS="PREV"),
    )

    await PlexDlnaAdapter._sm6_skip_via_key(adapter, previous=True)

    adapter._sm6_bump_queue_offset.assert_awaited_once_with(-1)
    adapter._sm6_publish_track_change.assert_called_once()
    adapter._sm6_enter_playing.assert_called_once_with(position="0")
