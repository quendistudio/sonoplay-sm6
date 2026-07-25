"""Plex UI STOP must always reach SM6 hardware (including post-reconnect passive sync)."""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# Stub heavy deps (same pattern as test_play_media_key).
def _make_stub(name):
    mod = types.ModuleType(name)
    mod.__path__ = []
    mod.__file__ = f"<stub {name}>"
    mod.__getattr__ = lambda attr: MagicMock()
    return mod


_STUB_MODULES = [
    "dotmap",
    "aiohttp",
    "aiohttp.ClientConnectionError",
    "xmltodict",
    "uvicorn",
    "starlette",
    "starlette.datastructures",
    "fastapi",
    "fastapi.responses",
    "fastapi.templating",
    "fastapi.staticfiles",
    "pydantic",
    "pydantic.settings",
    "jinja2",
    "utils",
    "settings",
    "version",
    "plex.play_queue",
    "plex.subscribe",
    "plex.gdm",
    "plex.pin_login",
    "plex.plexserver",
    "dlna",
    "dlna.dlna_device",
    "dlna.discover",
    "dlna.virtual",
    "dlna.virtual.devices",
    "dlna.virtual.volume",
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
sys.modules["plex.play_queue"].PlayQueue = MagicMock
sys.modules["dotmap"].DotMap = MagicMock
_sm6_control = types.ModuleType("dlna.sm6_control")
_sm6_control.Sm6Control = MagicMock
sys.modules["dlna.sm6_control"] = _sm6_control

from plex.adapters import PlexDlnaAdapter  # noqa: E402


def _stop_adapter(*, owned: bool, relinquished: bool) -> PlexDlnaAdapter:
    adapter = object.__new__(PlexDlnaAdapter)
    adapter.dlna = MagicMock()
    adapter.dlna.name = "SM6"
    adapter.dlna.location_url = "http://sm6.example:8050/description.xml"

    adapter.state = MagicMock()
    adapter.state.update = MagicMock()
    adapter.state.check_all_next_loop = False

    adapter.virtual_controller = MagicMock(return_value=None)
    adapter.queue = MagicMock()
    adapter.current_track_info = MagicMock()
    adapter._sm6_session_uri = "http://track"
    adapter._transport_cancel_requested = False
    adapter._transport_lock = asyncio.Lock()
    adapter._active_operation_id = None
    adapter._active_operation_event = None
    adapter._suppress_auto_next = False
    adapter._last_operation_finish_time = None
    adapter._auto_next_in_flight = False
    adapter._sm6_sonoplay_owned_playback = owned
    adapter._sm6_relinquished_control = relinquished
    adapter.wait_state_change_events = []
    adapter._is_sm6_renderer = MagicMock(return_value=True)
    adapter.loop = None

    adapter._sm6_clear_optimistic_play = MagicMock()
    adapter._sm6_relinquish_control = MagicMock()
    adapter._finish_transport_operation = MagicMock()
    adapter._sm6_run_control = AsyncMock()
    adapter._sm6_control = MagicMock(return_value=MagicMock(stop=AsyncMock()))
    return adapter


@pytest.mark.parametrize(
    ("owned", "relinquished"),
    [
        (False, False),
        (False, True),
        (True, True),
    ],
)
@pytest.mark.asyncio
async def test_plex_stop_reaches_sm6_after_passive_sync(owned, relinquished) -> None:
    adapter = _stop_adapter(owned=owned, relinquished=relinquished)

    await adapter.stop()

    adapter._sm6_relinquish_control.assert_called_once_with("plex_stop")
    assert adapter._sm6_sonoplay_owned_playback is False
    adapter._sm6_run_control.assert_awaited_once()
    assert adapter._sm6_run_control.await_args.kwargs.get("label") == "KeyPressed STOP"
