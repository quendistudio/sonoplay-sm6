"""Tests Plexamp volume step ↔ SM6 volume steps."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

_dlna_pkg = types.ModuleType("dlna")
_dlna_pkg.__path__ = [str(_root / "dlna")]
sys.modules.setdefault("dlna", _dlna_pkg)

for name, filename in (
    ("dlna.volume_curve", "volume_curve.py"),
    ("dlna.sm6_volume", "sm6_volume.py"),
):
    spec = importlib.util.spec_from_file_location(name, _root / "dlna" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

sm6_volume = sys.modules["dlna.sm6_volume"]
volume_curve = sys.modules["dlna.volume_curve"]

plex_to_step = sm6_volume.plex_to_step
plex_for_step = sm6_volume.plex_for_step
plexamp_step_volume_percent = sm6_volume.plexamp_step_volume_percent
plexamp_volume_step = sm6_volume.plexamp_volume_step
device_step = volume_curve.device_step
VOLUME_STEPS = volume_curve.VOLUME_STEPS


def test_plex_for_step_endpoints() -> None:
    assert plex_for_step(0) == 0
    assert plex_for_step(VOLUME_STEPS) == 100


def test_plexamp_step_up_one_step() -> None:
    current = plex_for_step(14)
    requested = current + 4
    stepped = plexamp_step_volume_percent(
        current, requested, hardware_step=14, max_step_delta=8,
    )
    assert stepped == plex_for_step(15)


def test_plexamp_step_down_one_step() -> None:
    current = plex_for_step(14)
    requested = current - 4
    stepped = plexamp_step_volume_percent(
        current, requested, hardware_step=14, max_step_delta=8,
    )
    assert stepped == plex_for_step(13)


def test_plexamp_step_from_mute_to_step_one() -> None:
    stepped = plexamp_step_volume_percent(0, 4, hardware_step=0, max_step_delta=8)
    assert stepped == plex_for_step(1)


def test_plexamp_step_large_delta_uses_absolute() -> None:
    assert plexamp_step_volume_percent(20, 60, max_step_delta=8) is None


def test_plexamp_step_zero_delta_unchanged() -> None:
    assert plexamp_volume_step(50, 50, hardware_step=14, max_step_delta=8) == 14
    assert plexamp_volume_step(50, 50, hardware_step=None, max_step_delta=8) is None


def test_plexamp_volume_step_from_mute() -> None:
    assert plexamp_volume_step(0, 4, hardware_step=0, max_step_delta=8) == 1


def test_plexamp_volume_step_at_max() -> None:
    from dlna.volume_curve import VOLUME_STEPS

    current = plex_for_step(VOLUME_STEPS)
    assert plexamp_volume_step(
        current, current + 3, hardware_step=VOLUME_STEPS, max_step_delta=8,
    ) == VOLUME_STEPS


def test_plexamp_step_uses_hardware_step_when_plex_cache_stale() -> None:
    """After idle, Plex UI may say 22% while SM6 is on step 21 (50%)."""
    stale_plex = plex_for_step(15)  # 22%
    requested = stale_plex + 1
    step = plexamp_volume_step(
        stale_plex,
        requested,
        hardware_step=21,
        max_step_delta=8,
    )
    assert step == 22
    assert plex_for_step(step) == plex_for_step(22)


@pytest.fixture
def settings_stub(monkeypatch):
    settings = types.SimpleNamespace(
        sm6_plexamp_volume_step_enabled=True,
        sm6_plexamp_volume_step_max_delta=8,
    )
    monkeypatch.setitem(sys.modules, "settings", types.ModuleType("settings"))
    sys.modules["settings"].settings = settings
    return settings


def _load_plex_client():
    spec = importlib.util.spec_from_file_location(
        "plex.plex_client",
        _root / "plex" / "plex_client.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_is_plexamp_client_by_product(settings_stub) -> None:
    plex_client = _load_plex_client()
    assert plex_client.is_plexamp_client(product="Plexamp")
    assert not plex_client.is_plexamp_client(product="Plex for Windows")


def test_resolve_sm6_volume_only_for_plexamp(settings_stub) -> None:
    plex_client = _load_plex_client()
    adapter = MagicMock()
    adapter._is_sm6_renderer.return_value = True
    adapter.state.volume = plex_for_step(10)

    unchanged, step = plex_client.resolve_sm6_volume_for_client(
        adapter,
        plex_for_step(11) + 3,
        product="Plex for Windows",
        device_step=10,
    )
    assert unchanged == plex_for_step(11) + 3
    assert step is None

    stepped, stepped_step = plex_client.resolve_sm6_volume_for_client(
        adapter,
        plex_for_step(10) + 4,
        product="Plexamp",
        device_step=10,
    )
    assert stepped_step == 11
    assert stepped == plex_for_step(11)


def test_resolve_sm6_volume_derives_step_from_cache(settings_stub) -> None:
    """No GetVolume / device_step: Plexamp +/- uses plex_to_step(state.volume)."""
    plex_client = _load_plex_client()
    adapter = MagicMock()
    adapter._is_sm6_renderer.return_value = True
    adapter.state.volume = plex_for_step(10)

    stepped, stepped_step = plex_client.resolve_sm6_volume_for_client(
        adapter,
        plex_for_step(10) + 4,
        product="Plexamp",
    )
    assert stepped_step == 11
    assert stepped == plex_for_step(11)


def test_resolve_sm6_volume_zero_delta_is_noop(settings_stub) -> None:
    plex_client = _load_plex_client()
    adapter = MagicMock()
    adapter._is_sm6_renderer.return_value = True
    adapter.state.volume = 22

    unchanged, step = plex_client.resolve_sm6_volume_for_client(
        adapter,
        22,
        product="Plexamp",
        device_step=8,
    )
    assert unchanged == 22
    assert step is None
