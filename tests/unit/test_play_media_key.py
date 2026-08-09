"""Unit tests for PlexDlnaAdapter.play_media() track key selection."""

import asyncio
import sys
import types
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Stub out heavy transitive dependencies (same pattern as test_adapter_stopped_state)
# ---------------------------------------------------------------------------

def _make_stub(name):
    mod = types.ModuleType(name)
    mod.__path__ = []
    mod.__file__ = f"<stub {name}>"
    mod.__getattr__ = lambda attr: MagicMock()
    return mod


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
def _rating_key_from_metadata_key(key: str):
    rating_from_key = str(key).rstrip("/").rsplit("/", 1)[-1]
    return rating_from_key if rating_from_key.isdigit() else None


_PlayQueue = MagicMock()
_PlayQueue._rating_key_from_metadata_key = staticmethod(_rating_key_from_metadata_key)
sys.modules["plex.play_queue"].PlayQueue = _PlayQueue
sys.modules["dotmap"].DotMap = MagicMock
sys.modules["starlette.datastructures"].QueryParams = MagicMock

from plex.adapters import PlexDlnaAdapter  # noqa: E402


@pytest.fixture
def play_media_adapter():
    adapter = object.__new__(PlexDlnaAdapter)
    adapter.dlna = MagicMock()
    adapter.dlna.name = "SM6"
    adapter.dlna.get_data = AsyncMock()

    adapter.state = MagicMock()
    adapter.state.touch_access_time = MagicMock()
    adapter.state.update = MagicMock()

    adapter.virtual_controller = MagicMock(return_value=None)
    adapter.plex_lib = MagicMock()
    adapter.plex_lib.update = MagicMock()

    queue = MagicMock()
    queue.get_info = AsyncMock()
    queue.select_track_key = AsyncMock()
    # play_media always awaits these before key selection.
    queue.selected_offset = AsyncMock(return_value=0)
    queue.selected_track = AsyncMock(
        return_value=SimpleNamespace(ratingKey="", title="?", index=None),
    )
    adapter.plex_lib.get_queue.return_value = queue
    adapter.queue = queue

    adapter.play_selected_queue_item = AsyncMock()
    adapter._is_sm6_renderer = lambda: False
    return adapter


def test_play_media_selects_track_key_when_provided(play_media_adapter):
    asyncio.run(play_media_adapter.play_media(
        "/playQueues/123",
        key="/library/metadata/900003",
        offset=5000,
        paused=True,
    ))

    play_media_adapter.plex_lib.get_queue.assert_called_once_with("/playQueues/123")
    play_media_adapter.queue.get_info.assert_awaited_once()
    play_media_adapter.queue.select_track_key.assert_awaited_once_with("/library/metadata/900003")
    play_media_adapter.play_selected_queue_item.assert_awaited_once_with(offset=5000, paused=True)


def test_play_media_skips_select_track_key_when_key_missing(play_media_adapter):
    asyncio.run(play_media_adapter.play_media("/playQueues/123", offset=0))

    play_media_adapter.queue.select_track_key.assert_not_called()
    play_media_adapter.play_selected_queue_item.assert_awaited_once_with(offset=0, paused=False)


def test_play_media_playqueue_album_key_no_album_fallback(play_media_adapter):
    play_media_adapter._is_sm6_renderer = lambda: True
    play_media_adapter._sm6_begin_plex_play = MagicMock(return_value=1)
    play_media_adapter._sm6_plex_play_epoch = 1
    play_media_adapter._sm6_plex_play_in_progress = False
    play_media_adapter._sm6_abort_plex_play_takeover = MagicMock()
    play_media_adapter.queue.select_track_key = AsyncMock(return_value=False)
    play_media_adapter.plex_lib.fetch_metadata = AsyncMock(
        return_value=SimpleNamespace(type="album", title="Thematic Album"),
    )
    play_media_adapter._play_sm6_album = AsyncMock()

    asyncio.run(play_media_adapter.play_media(
        "/playQueues/456",
        key="/library/metadata/800001",
    ))

    play_media_adapter._play_sm6_album.assert_not_called()
    play_media_adapter.play_selected_queue_item.assert_not_called()
    play_media_adapter._sm6_abort_plex_play_takeover.assert_called_once()


def test_play_media_stale_epoch_skips_abort(play_media_adapter):
    """Older playMedia finally must not abort after a newer playMedia began."""
    play_media_adapter._is_sm6_renderer = lambda: True
    play_media_adapter._sm6_begin_plex_play = MagicMock(return_value=1)
    play_media_adapter._sm6_plex_play_epoch = 2
    play_media_adapter._sm6_plex_play_in_progress = True
    play_media_adapter._sm6_abort_plex_play_takeover = MagicMock()
    play_media_adapter.queue.select_track_key = AsyncMock(return_value=False)

    asyncio.run(play_media_adapter.play_media(
        "/playQueues/456",
        key="/library/metadata/800001",
    ))

    play_media_adapter._sm6_abort_plex_play_takeover.assert_not_called()
    assert play_media_adapter._sm6_plex_play_in_progress is True


def test_play_media_non_playqueue_album_key_still_uses_album_fallback(play_media_adapter):
    play_media_adapter._is_sm6_renderer = lambda: True
    play_media_adapter._sm6_begin_plex_play = MagicMock(return_value=1)
    play_media_adapter._sm6_plex_play_epoch = 1
    play_media_adapter._sm6_plex_play_in_progress = False
    play_media_adapter._sm6_abort_plex_play_takeover = MagicMock()
    play_media_adapter.queue.select_track_key = AsyncMock(return_value=False)
    album = SimpleNamespace(type="album", title="Direct Album")
    play_media_adapter.plex_lib.fetch_metadata = AsyncMock(return_value=album)
    play_media_adapter._play_sm6_album = AsyncMock()

    asyncio.run(play_media_adapter.play_media(
        "/library/metadata/800001",
        key="/library/metadata/800001",
    ))

    play_media_adapter._play_sm6_album.assert_awaited_once()
    play_media_adapter.play_selected_queue_item.assert_not_called()
