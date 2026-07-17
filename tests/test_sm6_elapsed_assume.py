"""Tests SM6 position interpolation between SOAP polls."""

import asyncio
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


class _DotMap(dict):
    def __init__(self, *args, **kwargs):
        super().__init__()
        object.__setattr__(self, "old", types.SimpleNamespace())
        if args:
            self.update(args[0])
        self.update(kwargs)

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key, value):
        if key == "old":
            object.__setattr__(self, key, value)
        else:
            self[key] = value

    def toDict(self):
        return dict(self)


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

_settings = MagicMock()
_settings.sm6_position_plex_notify_min_delta_ms = 1000
_settings.sm6_position_assume_play_delay_seconds = 0.1
_settings.sm6_position_assume_skip_delay_seconds = 0.35
_settings.sm6_position_resync_back_tolerance_ms = 500
_settings.sm6_optimistic_play_seconds = 2.5
_settings.sm6_play_timeline_push_interval_seconds = 0.25
sys.modules["settings"].settings = _settings

_utils = sys.modules["utils"]
_utils.parse_timedelta = MagicMock(return_value=0)
_utils.convert_volume = MagicMock(return_value=50)
_utils.g = MagicMock()
_utils.pms_header = MagicMock(return_value={})
_utils.extract_value = MagicMock()

sys.modules["dotmap"].DotMap = _DotMap
sys.modules["plex.play_queue"].PlayQueue = MagicMock
sys.modules["starlette.datastructures"].QueryParams = MagicMock

from plex.adapters import DlnaState, PlexDlnaAdapter  # noqa: E402

DotMap = _DotMap


@pytest.fixture
def dlna_state():
    adapter = MagicMock()
    adapter.dlna = MagicMock(name="SM6")
    with patch.object(DlnaState, "start_looping"):
        state = DlnaState(adapter, state_change_callback=None)
    state._state = "PLAYING"
    state._elapsed = 10_000
    state._current_track_duration = 300_000
    return state


def test_continuous_extrapolation_live_between_plex_ticks(dlna_state):
    with patch("plex.adapters.time.monotonic", side_effect=[100.0, 100.6, 100.6, 100.6, 100.6]):
        dlna_state._sync_elapsed_anchor(10_000)
        dlna_state._refresh_assumed_elapsed()
        assert dlna_state.elapsed == 10_000
        assert dlna_state.live_elapsed_ms() == 10_600


def test_continuous_extrapolation_notifies_plex_on_ms_delta(dlna_state):
    with patch("plex.adapters.time.monotonic", side_effect=[100.0, 101.1, 101.1]):
        dlna_state._sync_elapsed_anchor(10_000)
        dlna_state._refresh_assumed_elapsed()
    assert dlna_state.elapsed == 11_100


def test_extrapolation_keeps_subsecond_ms_not_quantized(dlna_state):
    with patch("plex.adapters.time.monotonic", side_effect=[100.0, 100.347, 100.347]):
        dlna_state._sync_elapsed_anchor(10_000)
        assert dlna_state.live_elapsed_ms() == 10_347
    with patch("plex.adapters.time.monotonic", side_effect=[100.0, 101.05, 101.05]):
        dlna_state._sync_elapsed_anchor(10_000)
        dlna_state._refresh_assumed_elapsed()
    assert dlna_state.elapsed == 11_050


def test_arm_elapsed_assume_waits_for_play_delay(dlna_state):
    with patch("plex.adapters.time.monotonic", side_effect=[100.0, 100.05]):
        dlna_state._arm_elapsed_assume_impl()
        dlna_state._refresh_assumed_elapsed()
    assert dlna_state.elapsed == 10_000


def test_arm_elapsed_assume_ticks_after_play_delay(dlna_state):
    with patch("plex.adapters.time.monotonic", side_effect=[100.0, 101.2, 101.2]):
        dlna_state._arm_elapsed_assume_impl()
        dlna_state._refresh_assumed_elapsed()
    assert dlna_state.elapsed == 11_100


def test_resync_anchors_live_ms_on_small_backward_jitter(dlna_state):
    dlna_state._elapsed = 10_800
    with patch("plex.adapters.time.monotonic", side_effect=[100.0, 100.3, 100.3, 100.3]):
        dlna_state._sync_elapsed_anchor(10_800)
        dlna_state._resync_elapsed_from_sm6(10_500)
    assert dlna_state.elapsed == 10_800
    assert dlna_state._elapsed_anchor_ms == 11_100


def test_resync_applies_large_backward_correction(dlna_state):
    dlna_state._elapsed = 12_000
    dlna_state._sync_elapsed_anchor(12_000)
    dlna_state._resync_elapsed_from_sm6(10_000)
    assert dlna_state.elapsed == 10_000


def test_disarm_elapsed_assume_stops_extrapolation(dlna_state):
    dlna_state._arm_elapsed_assume_impl()
    dlna_state._disarm_elapsed_assume_impl()
    with patch("plex.adapters.time.monotonic", side_effect=[100.0, 101.0]):
        dlna_state._refresh_assumed_elapsed()
    assert dlna_state.elapsed == 10_000


def test_elapsed_jump_wakes_timeline_on_one_second_tick():
    adapter = MagicMock(spec=PlexDlnaAdapter)
    event = asyncio.Event()
    adapter.wait_state_change_events = [
        {"event": event, "interesting_fields": ["elapsed_jump"]},
    ]
    adapter._update_stats = MagicMock()

    changed = DotMap(elapsed=11_000)
    changed.old.elapsed = 10_000

    asyncio.run(PlexDlnaAdapter.state_changed(adapter, changed))
    assert event.is_set()


def test_optimistic_play_keeps_playing_during_sm6_pause_lag():
    adapter = MagicMock()
    adapter._sm6_optimistic_play_until = 200.0
    adapter.dlna = MagicMock(name="SM6")

    with patch("plex.adapters.time.monotonic", return_value=100.0):
        from plex.adapters import PlexDlnaAdapter

        real = PlexDlnaAdapter.__new__(PlexDlnaAdapter)
        real._sm6_optimistic_play_until = 200.0
        assert real._sm6_should_accept_transport_state("PAUSED_PLAYBACK", "PLAYING") == "PLAYING"


def test_optimistic_play_clears_on_sm6_playing_confirm():
    from plex.adapters import PlexDlnaAdapter

    real = PlexDlnaAdapter.__new__(PlexDlnaAdapter)
    real._sm6_optimistic_play_until = 200.0
    assert real._sm6_should_accept_transport_state("PLAYING", "PLAYING") == "PLAYING"
    assert real._sm6_optimistic_play_until is None


def test_begin_optimistic_play_arms_without_delay():
    from plex.adapters import PlexDlnaAdapter

    adapter = MagicMock()
    adapter._is_sm6_renderer = MagicMock(return_value=True)
    adapter.state = MagicMock()
    adapter.loop = None
    adapter._sm6_play_notify_task = None
    adapter.dlna = MagicMock(name="SM6")

    real = PlexDlnaAdapter.__new__(PlexDlnaAdapter)
    real._is_sm6_renderer = adapter._is_sm6_renderer
    real.state = adapter.state
    real.loop = adapter.loop
    real._sm6_play_notify_task = None
    real.dlna = adapter.dlna
    real._sm6_optimistic_play_until = None

    with patch("plex.adapters.time.monotonic", return_value=100.0):
        with patch("plex.adapters.settings") as mock_settings:
            mock_settings.sm6_optimistic_play_seconds = 2.5
            real._sm6_begin_optimistic_play()
    adapter.state.arm_elapsed_assume.assert_called_once_with(delay_seconds=0)
    assert real._sm6_optimistic_play_until == 102.5
