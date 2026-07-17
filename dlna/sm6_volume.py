"""Volume: Plex 0-100 ↔ device 0-1 ↔ UPnP level (renderer-declared range)."""
from __future__ import annotations

from dataclasses import dataclass

from dlna.volume_curve import device_to_ui, ui_to_device


@dataclass(frozen=True, slots=True)
class VolumeRange:
    """RenderingControl Volume allowedValueRange (from device SCPD, cached at load)."""

    minimum: int = 0
    maximum: int = 100
    step: int = 1

    @classmethod
    def from_device(cls, device) -> VolumeRange:
        return cls(
            minimum=int(device.volume_min if device.volume_min is not None else 0),
            maximum=int(device.volume_max if device.volume_max is not None else 100),
            step=int(device.volume_step if device.volume_step is not None else 1),
        )

    @property
    def span(self) -> int:
        return self.maximum - self.minimum


# --- Plex 0-100 ↔ device 0-1 (perceptual curve) --------------------------------


def plex_to_device(percent: int | float) -> float:
    """Plex slider 0-100 → device amplitude 0-1."""
    ui = max(0.0, min(100.0, float(percent))) / 100.0
    return ui_to_device(ui)


def device_to_plex(device_level: float) -> int:
    """Device amplitude 0-1 → Plex slider 0-100."""
    ui = device_to_ui(max(0.0, min(1.0, float(device_level))))
    return int(round(ui * 100))


# --- device 0-1 ↔ UPnP level (linear over declared range) -----------------------


def dlna_level_to_device(raw, volume_range: VolumeRange) -> float:
    """UPnP CurrentVolume / DesiredVolume → device amplitude 0-1."""
    if raw is None:
        return 0.0
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    span = volume_range.span
    if span <= 0:
        return 0.0
    clamped = max(float(volume_range.minimum), min(float(volume_range.maximum), value))
    return (clamped - volume_range.minimum) / span


def device_to_dlna_level(device_level: float, volume_range: VolumeRange) -> int:
    """Device amplitude 0-1 → UPnP DesiredVolume (truncated integer)."""
    level = max(0.0, min(1.0, float(device_level)))
    span = volume_range.span
    if span <= 0:
        return volume_range.minimum
    return max(
        volume_range.minimum,
        min(volume_range.maximum, int(volume_range.minimum + level * span)),
    )


def plex_to_dlna_level(percent: int | float, volume_range: VolumeRange) -> int:
    """Plex slider 0-100 → UPnP DesiredVolume via calibrated curve."""
    return device_to_dlna_level(plex_to_device(percent), volume_range)


def dlna_level_to_plex(raw, volume_range: VolumeRange) -> int:
    """UPnP CurrentVolume → Plex slider 0-100."""
    return device_to_plex(dlna_level_to_device(raw, volume_range))


# --- SM6 volume step helpers (28 discrete steps; Plexamp stepping) --------------


def device_to_step(device_level: float) -> int:
    """Device amplitude 0-1 → SM6 volume step 0..28."""
    from dlna.volume_curve import _step_for_device

    return _step_for_device(max(0.0, min(1.0, float(device_level))))


def plex_to_step(percent: int | float) -> int:
    """Plex slider 0-100 → SM6 volume step 0..28 (0 = mute)."""
    return device_to_step(plex_to_device(percent))


def plex_for_step(step: int) -> int:
    """SM6 volume step 0..28 → Plex slider 0-100 (best available approximation)."""
    from dlna.volume_curve import VOLUME_STEPS, device_step

    step = max(0, min(VOLUME_STEPS, int(step)))
    if step == 0:
        return 0
    percent = device_to_plex(device_step(step))
    for candidate in range(max(1, percent), 101):
        if plex_to_step(candidate) == step:
            return candidate
    return max(percent, 1)


def step_to_dlna_level(step: int, volume_range: VolumeRange) -> int:
    """Volume step 0..28 → UPnP DesiredVolume (step index when range is 0..28)."""
    from dlna.volume_curve import VOLUME_STEPS, device_step

    step = max(0, min(VOLUME_STEPS, int(step)))
    if step <= 0:
        return volume_range.minimum
    if volume_range.minimum == 0 and volume_range.maximum == VOLUME_STEPS:
        return step
    return device_to_dlna_level(device_step(step), volume_range)


def plexamp_volume_step(
    current_percent: int,
    requested_percent: int,
    *,
    hardware_step: int | None = None,
    max_step_delta: int = 8,
) -> int | None:
    """±1 volume step for Plexamp +/-; None = use absolute Plex mapping (slider drag).

    Requires hardware_step from GetVolume — no perceptual curve on button presses.
    """
    current = int(current_percent)
    requested = int(requested_percent)
    delta = requested - current
    if delta == 0:
        return hardware_step
    if abs(delta) > max_step_delta:
        return None
    if hardware_step is None:
        return None

    if delta > 0:
        from dlna.volume_curve import VOLUME_STEPS

        return min(VOLUME_STEPS, hardware_step + 1) if hardware_step > 0 else 1
    return max(0, hardware_step - 1)


def plexamp_step_volume_percent(
    current_percent: int,
    requested_percent: int,
    *,
    hardware_step: int | None = None,
    max_step_delta: int = 8,
) -> int | None:
    """Alias returning Plex %; prefer plexamp_volume_step + set_volume(sm6_step=)."""
    step = plexamp_volume_step(
        current_percent,
        requested_percent,
        hardware_step=hardware_step,
        max_step_delta=max_step_delta,
    )
    if step is None:
        return None
    return plex_for_step(step)
