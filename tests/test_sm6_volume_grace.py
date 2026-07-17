"""Tests SM6 volume grace period (no stale Plex zero on play start)."""

import time


class _VolumeGraceStub:
    def __init__(self) -> None:
        self.dlna = type("D", (), {"name": "SM6-test"})()
        self._sm6_volume_grace_until = None
        self.state = type("S", (), {"volume": 85})()

    def _is_sm6_renderer(self) -> bool:
        return True

    def _sm6_note_playback_start(self, seconds: float = 5.0) -> None:
        self._sm6_volume_grace_until = time.monotonic() + seconds

    def _sm6_in_volume_grace_period(self) -> bool:
        return (
            self._sm6_volume_grace_until is not None
            and time.monotonic() < self._sm6_volume_grace_until
        )

    def _sm6_should_ignore_volume_write(self, requested: int) -> bool:
        if not self._is_sm6_renderer() or not self._sm6_in_volume_grace_period():
            return False
        device_plex = self.state.volume
        if device_plex is None:
            return False
        device_plex = int(device_plex)
        requested = int(requested)
        if requested == 0 and device_plex > 5:
            return True
        if abs(requested - device_plex) > 20:
            return True
        return False


def test_ignores_stale_zero_during_grace() -> None:
    stub = _VolumeGraceStub()
    stub._sm6_note_playback_start(5.0)
    assert stub._sm6_should_ignore_volume_write(0) is True


def test_allows_matching_volume_during_grace() -> None:
    stub = _VolumeGraceStub()
    stub.state.volume = 85
    stub._sm6_note_playback_start(5.0)
    assert stub._sm6_should_ignore_volume_write(85) is False


def test_allows_volume_after_grace_expired() -> None:
    stub = _VolumeGraceStub()
    stub._sm6_volume_grace_until = time.monotonic() - 0.01
    assert stub._sm6_should_ignore_volume_write(0) is False
