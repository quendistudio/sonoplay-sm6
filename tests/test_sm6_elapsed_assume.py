"""Tests SM6 position interpolation between SOAP polls."""

import asyncio
import sys
import time
import types
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import bare_sm6_adapter, drop_stub_modules, purge_settings_modules


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
_settings.sm6_position_plex_notify_min_delta_ms = 300
_settings.sm6_position_assume_play_delay_seconds = 0.1
_settings.sm6_position_assume_skip_delay_seconds = 0.35
_settings.sm6_position_resync_back_tolerance_ms = 1000
_settings.sm6_play_timeline_push_interval_seconds = 0.25
_settings.sm6_force_poll_debounce_seconds = 0.0
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

# Do not leave import stubs for other test modules that need real packages.
drop_stub_modules()
purge_settings_modules()

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
    # Stay under sm6_position_plex_notify_min_delta_ms (300) so refresh does not commit.
    with patch("plex.adapters.time.monotonic", side_effect=[100.0, 100.2, 100.2, 100.2, 100.2]):
        dlna_state._sync_elapsed_anchor(10_000)
        dlna_state._refresh_assumed_elapsed()
        assert dlna_state.elapsed == 10_000
        assert dlna_state.live_elapsed_ms() == 10_200


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


def test_begin_optimistic_play_enables_timeline_extrapolation():
    from plex.adapters import PlexDlnaAdapter

    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6")
    adapter.loop = None
    adapter._sm6_play_notify_task = None
    adapter._is_sm6_renderer = lambda: True
    adapter._sm6_mark_outbound_activity = MagicMock()

    with patch.object(DlnaState, "start_looping"):
        adapter.state = DlnaState(adapter)
    adapter.state._state = "TRANSITIONING"

    with patch("plex.adapters.time.monotonic", return_value=100.0):
        adapter._sm6_begin_optimistic_play(elapsed_ms=0, delay_seconds=0)
    assert adapter.state._elapsed_assume_active is True
    assert adapter.state.elapsed == 0
    assert adapter.state._elapsed_anchor_mono == 100.0
    adapter._sm6_mark_outbound_activity.assert_called_once()
    # No event loop → pusher task not started; active means task running.
    assert adapter._sm6_optimistic_play_active() is False


def test_enter_playing_does_not_arm_elapsed():
    from plex.adapters import PlexDlnaAdapter

    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6")
    adapter.loop = None
    adapter._sm6_play_notify_task = None
    adapter._is_sm6_renderer = lambda: True
    adapter._sm6_mark_outbound_activity = MagicMock()
    adapter._sm6_begin_optimistic_play = MagicMock()

    with patch.object(DlnaState, "start_looping"):
        adapter.state = DlnaState(adapter)
    adapter.state.update = MagicMock()
    adapter.state._elapsed_assume_active = False

    adapter._sm6_enter_playing(position="0")
    adapter.state.update.assert_called_once_with(state="PLAYING", position="0")
    adapter._sm6_begin_optimistic_play.assert_not_called()


def test_begin_plex_play_arms_with_play_delay():
    from plex.adapters import PlexDlnaAdapter

    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6")
    adapter.loop = None
    adapter._sm6_play_notify_task = None
    adapter._is_sm6_renderer = lambda: True
    adapter._sm6_sonoplay_owned_playback = False
    adapter._sm6_relinquished_control = True
    adapter._sm6_mark_outbound_activity = MagicMock()

    with patch.object(DlnaState, "start_looping"):
        adapter.state = DlnaState(adapter)
    adapter.state.update = MagicMock()

    with patch("plex.adapters.time.monotonic", return_value=50.0):
        adapter._sm6_begin_plex_play()
    assert adapter.state._elapsed_assume_active is True
    assert adapter.state._elapsed_anchor_mono == pytest.approx(50.0 + 0.1)
    adapter.state.update.assert_called_once_with(state="TRANSITIONING")


def test_skip_rearms_with_skip_delay():
    from plex.adapters import PlexDlnaAdapter

    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6")
    adapter.loop = None
    adapter._sm6_play_notify_task = None
    adapter._is_sm6_renderer = lambda: True
    adapter._sm6_mark_outbound_activity = MagicMock()

    with patch.object(DlnaState, "start_looping"):
        adapter.state = DlnaState(adapter)
    adapter.state._state = "PLAYING"
    adapter.state.elapsed = 12_000

    with patch("plex.adapters.time.monotonic", return_value=200.0):
        adapter._sm6_clear_optimistic_play()
        adapter.state.disarm_elapsed_assume()
        adapter._sm6_begin_optimistic_play(
            elapsed_ms=0,
            delay_seconds=_settings.sm6_position_assume_skip_delay_seconds,
        )
    assert adapter.state.elapsed == 0
    assert adapter.state._elapsed_anchor_mono == pytest.approx(200.0 + 0.35)


def test_accept_transport_wires_optimistic_play_not_outbound():
    """Pause masking must follow the elapsed pusher, not the outbound SOAP window."""
    from plex.adapters import PlexDlnaAdapter

    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter._sm6_outbound_until = None
    adapter._sm6_clear_optimistic_play = MagicMock()
    adapter.state = MagicMock()
    adapter.state.disarm_elapsed_assume = MagicMock()

    task = MagicMock()
    task.done.return_value = False
    adapter._sm6_play_notify_task = task
    assert adapter._sm6_should_accept_transport_state("PAUSED_PLAYBACK", "PLAYING") == "PLAYING"
    adapter._sm6_clear_optimistic_play.assert_not_called()

    adapter._sm6_play_notify_task = None
    assert adapter._sm6_should_accept_transport_state("PAUSED_PLAYBACK", "PLAYING") == "PAUSED_PLAYBACK"
    adapter._sm6_clear_optimistic_play.assert_called_once()
    adapter.state.disarm_elapsed_assume.assert_called_once()


def test_elapsed_pusher_runs_on_assume_without_playing_state():
    """Pusher follows assume arming, not SM6 PLAYING confirmation."""
    from plex.adapters import PlexDlnaAdapter

    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6")
    adapter.loop = None
    adapter.no_notice = True
    adapter.wait_state_change_events = []
    adapter._sm6_play_notify_task = MagicMock()
    adapter._is_sm6_renderer = lambda: True
    adapter._sm6_mark_outbound_activity = MagicMock()

    with patch.object(DlnaState, "start_looping"):
        adapter.state = DlnaState(adapter)
    adapter.state._state = "TRANSITIONING"
    adapter.state._arm_elapsed_assume_impl(elapsed_ms=0, delay_seconds=0)

    async def _run():
        task = asyncio.create_task(adapter._sm6_play_elapsed_pusher())
        await asyncio.sleep(0.12)
        assert not task.done()
        adapter.state._disarm_elapsed_assume_impl()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_run())


def test_wake_waiters_can_skip_force_poll():
    from plex.adapters import PlexDlnaAdapter

    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6")
    adapter.loop = None
    adapter.no_notice = True
    adapter.wait_state_change_events = []
    with patch.object(DlnaState, "start_looping"):
        adapter.state = DlnaState(adapter)
    adapter.state._check_all_next_loop = False

    adapter._sm6_wake_waiters(force_poll=False)
    assert adapter.state._check_all_next_loop is False


def test_live_elapsed_extrapolates_during_transitioning():
    adapter = bare_sm6_adapter(PlexDlnaAdapter)
    adapter.dlna = MagicMock(name="SM6")
    adapter._is_sm6_renderer = lambda: True

    with patch.object(DlnaState, "start_looping"):
        state = DlnaState(adapter)
    state._state = "TRANSITIONING"

    with patch("plex.adapters.time.monotonic", return_value=100.0):
        state._arm_elapsed_assume_impl(elapsed_ms=0, delay_seconds=0)

    with patch("plex.adapters.time.monotonic", side_effect=[100.0, 101.2, 101.2]):
        assert state.live_elapsed_ms() == 1200


def test_live_elapsed_works_before_sm6_playing():
    """Assume clock must tick even while local transport is still STOPPED."""
    with patch.object(DlnaState, "start_looping"):
        state = DlnaState(MagicMock())
    state._state = "STOPPED"
    with patch("plex.adapters.time.monotonic", return_value=10.0):
        state._arm_elapsed_assume_impl(elapsed_ms=0, delay_seconds=0)
    with patch("plex.adapters.time.monotonic", side_effect=[10.0, 10.5, 10.5]):
        assert state.live_elapsed_ms() == 500


def test_resync_applies_after_skip_without_suppression(dlna_state):
    adapter = dlna_state.adapter
    adapter._is_sm6_renderer = lambda: True
    dlna_state._arm_elapsed_assume_impl(elapsed_ms=0, delay_seconds=0)
    dlna_state._resync_elapsed_from_sm6(1_000)
    assert dlna_state.elapsed == 1_000
