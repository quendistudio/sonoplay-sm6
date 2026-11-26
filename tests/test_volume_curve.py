"""Tests calibrated SM6 volume curve (step 0 = mute, 28 audible steps)."""

import pytest

from dlna.volume_curve import (
    PERCEPTUAL_DB_PER_HALVING,
    VOLUME_STEPS,
    _db_for_device,
    _device_for_db,
    _target_db_for_ui,
    device_step,
    device_to_ui,
    quantize_device_up,
    ui_to_device,
)


def test_ui_to_device_endpoints() -> None:
    assert ui_to_device(0.0) == 0.0
    assert ui_to_device(1.0) == 1.0


def test_device_to_ui_endpoints() -> None:
    assert device_to_ui(0.0) == 0.0
    assert device_to_ui(1.0) == 1.0


def test_step_zero_is_mute() -> None:
    assert device_step(0) == 0.0
    assert device_to_ui(0.0) == 0.0
    assert ui_to_device(0.0) == 0.0


def test_volume_steps_grid() -> None:
    assert device_step(1) == pytest.approx(1 / VOLUME_STEPS)
    assert device_step(28) == 1.0
    steps = [device_step(n) for n in range(1, VOLUME_STEPS + 1)]
    assert all(a < b for a, b in zip(steps, steps[1:], strict=False))


def test_halving_law_db() -> None:
    assert _target_db_for_ui(1.0) == pytest.approx(0.0)
    assert _target_db_for_ui(0.5) == pytest.approx(-PERCEPTUAL_DB_PER_HALVING)


def test_ui_to_device_halving_snap() -> None:
    assert ui_to_device(0.5) == device_step(21)
    assert ui_to_device(0.25) == device_step(16)


def test_calibration_anchors_db() -> None:
    assert _db_for_device(1 / VOLUME_STEPS) == pytest.approx(-88.0)
    assert _device_for_db(-79.0) == pytest.approx(2 / VOLUME_STEPS, abs=1e-9)


def test_device_to_ui_inverse_halving() -> None:
    assert device_to_ui(device_step(21)) == pytest.approx(0.5)
    assert device_to_ui(device_step(16)) == pytest.approx(0.25)


def test_curve_is_monotonic() -> None:
    samples = [ui_to_device(x / 100) for x in range(101)]
    assert all(a <= b for a, b in zip(samples, samples[1:], strict=False))


def test_quantize_device_up_snaps_to_grid() -> None:
    assert quantize_device_up(0.0) == 0.0
    assert quantize_device_up(0.1) == pytest.approx(3 / VOLUME_STEPS)
