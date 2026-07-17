"""Plex slider ↔ SM6 amplitude conversion, calibrated to dB display."""
from __future__ import annotations

import math

# 29 levels: step 0 = mute, steps 1..28 = n/28 (thousandth rounded up).
VOLUME_STEPS = 28

# dB removed at each halving of the slider (6 ≈ amplitude/2, ~10 ≈ perceived/2).
PERCEPTUAL_DB_PER_HALVING = 10.0

# Device amplitude (exact step) → SM6 displayed dB.
_CALIBRATION: tuple[tuple[float, float], ...] = (
    (0.0357142857142857, -88.0),    # step  1
    (0.0714285714285714, -79.0),    # step  2
    (0.1071428571428572, -70.0),    # step  3
    (0.1428571428571429, -66.0),    # step  4
    (0.1785714285714286, -62.0),    # step  5
    (0.2142857142857143, -58.0),    # step  6
    (0.25,               -54.0),    # step  7
    (0.2857142857142857, -50.0),    # step  8
    (0.3214285714285714, -46.0),    # step  9
    (0.3571428571428572, -42.0),    # step 10
    (0.3928571428571429, -38.0),    # step 11
    (0.4285714285714286, -34.0),    # step 12
    (0.4642857142857143, -30.0),    # step 13
    (0.5,                -26.0),    # step 14
    (0.5357142857142857, -22.0),    # step 15
    (0.5714285714285714, -20.0),    # step 16
    (0.6071428571428571, -18.0),    # step 17
    (0.6428571428571429, -16.0),    # step 18
    (0.6785714285714286, -14.0),    # step 19
    (0.7142857142857143, -12.0),    # step 20
    (0.75,               -10.0),    # step 21
    (0.7857142857142857,  -8.0),    # step 22
    (0.8214285714285714,  -6.0),    # step 23
    (0.8571428571428571,  -4.0),    # step 24
    (0.8928571428571429,  -3.0),    # step 25
    (0.9285714285714286,  -2.0),    # step 26
    (0.9642857142857143,  -1.0),    # step 27
    (1.0,                  0.0),    # step 28
)


def device_step(n: int) -> float:
    """Device value for step n (0 = mute, 1..28 = audible)."""
    if n <= 0:
        return 0.0
    if n >= VOLUME_STEPS:
        return 1.0
    return n / VOLUME_STEPS


def _target_db_for_ui(level: float) -> float:
    """Slider 0..1 → target dB: K × log₂(level), 100% = 0 dB."""
    return PERCEPTUAL_DB_PER_HALVING * math.log2(level)


def _ui_level_for_db(db: float) -> float:
    """SM6 displayed dB → slider position: 2^(db / K)."""
    if db >= 0.0:
        return 1.0
    return 2.0 ** (db / PERCEPTUAL_DB_PER_HALVING)


def _step_for_db(target_db: float) -> int:
    """Step 1..28 whose displayed dB is closest to target_db."""
    best_step = 1
    best_dist = float("inf")
    for i, (_, db) in enumerate(_CALIBRATION):
        dist = abs(db - target_db)
        step = i + 1
        if dist < best_dist or (
            dist == best_dist and db > _CALIBRATION[best_step - 1][1]
        ):
            best_dist = dist
            best_step = step
    return best_step


def _step_for_device(device: float) -> int:
    """Step 0..28 closest to device amplitude."""
    if device <= 0.0:
        return 0
    if device >= 1.0:
        return VOLUME_STEPS
    best_step = 1
    best_dist = float("inf")
    for i, (amp, _) in enumerate(_CALIBRATION):
        dist = abs(amp - device)
        step = i + 1
        if dist < best_dist or (
            dist == best_dist and amp > _CALIBRATION[best_step - 1][0]
        ):
            best_dist = dist
            best_step = step
    return best_step


def _db_for_device(device: float) -> float:
    """Device amplitude → SM6 displayed dB (nearest step)."""
    if device <= 0.0:
        return float("-inf")
    return _CALIBRATION[_step_for_device(device) - 1][1]


def _device_for_db(target_db: float) -> float:
    """Target dB → amplitude of nearest SM6 step."""
    return _CALIBRATION[_step_for_db(target_db) - 1][0]


def quantize_device_up(value: float) -> float:
    """Round up to the next SM6 step (0 = mute)."""
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    for n in range(1, VOLUME_STEPS + 1):
        step = device_step(n)
        if step >= value - 1e-16:
            return step
    return 1.0


def ceil_milli(value: float) -> float:
    """Historical alias: quantization on the 28-step grid."""
    return quantize_device_up(value)


def ui_to_device(level: float) -> float:
    """Slider 0..1 → device amplitude (nearest step in dB)."""
    if level <= 0.0:
        return 0.0
    if level >= 1.0:
        return 1.0
    target_db = _target_db_for_ui(level)
    min_db = _CALIBRATION[0][1]
    if target_db <= min_db:
        return _CALIBRATION[0][0]
    step = _step_for_db(target_db)
    return device_step(step)


def device_to_ui(device: float) -> float:
    """Device amplitude 0..1 → slider 0..1 (perceptual inverse)."""
    if device <= 0.0:
        return 0.0
    if device >= 1.0:
        return 1.0
    db = _db_for_device(device)
    return min(1.0, max(0.0, _ui_level_for_db(db)))
