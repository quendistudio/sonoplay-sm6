"""Tests Plexamp volume step ↔ SM6 volume steps."""

from unittest.mock import MagicMock

import pytest

from dlna.sm6_volume import (
    plex_for_step,
    plex_to_step,
    plexamp_step_volume_percent,
    plexamp_volume_step,
)
from dlna.volume_curve import VOLUME_STEPS
from plex import plex_client


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
def plexamp_settings(settings_override):
    return settings_override(
        sm6_plexamp_volume_step_enabled=True,
        sm6_plexamp_volume_step_max_delta=8,
    )


def test_is_plexamp_client_by_product(plexamp_settings) -> None:
    assert plex_client.is_plexamp_client(product="Plexamp")
    assert not plex_client.is_plexamp_client(product="Plex for Windows")


def test_resolve_sm6_volume_only_for_plexamp(plexamp_settings) -> None:
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


def test_resolve_sm6_volume_derives_step_from_cache(plexamp_settings) -> None:
    """No GetVolume / device_step: Plexamp +/- uses plex_to_step(state.volume)."""
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


def test_resolve_sm6_volume_zero_delta_is_noop(plexamp_settings) -> None:
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
