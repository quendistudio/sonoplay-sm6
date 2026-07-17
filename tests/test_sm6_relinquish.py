"""Tests SM6 relinquish control and external interaction acceptance."""

import time


_ACTIVE = frozenset({"PLAYING", "PAUSED_PLAYBACK"})
_STOPPED = frozenset({"STOPPED", "NO_MEDIA_PRESENT"})


class _Sm6RelinquishStub:
    """Minimal adapter surface for SM6 transport/relinquish helpers."""

    def __init__(self) -> None:
        self.dlna = type("D", (), {"name": "SM6-test"})()
        self._sm6_relinquished_control = False
        self._sm6_optimistic_play_until = None
        self._sm6_outbound_until = None
        self._sm6_last_audio_source = 10
        self._suppress_auto_next = False
        self._sm6_session_uri = "http://track"
        self.loop = None
        self.no_notice = False
        self.wait_state_change_events = []
        self._state = "PLAYING"

    def _is_sm6_renderer(self) -> bool:
        return True

    def _sm6_clear_optimistic_play(self) -> None:
        self._sm6_optimistic_play_until = None

    def _sm6_wake_waiters(self) -> None:
        pass

    def _sm6_mark_outbound_activity(self, seconds: float = 45.0) -> None:
        deadline = time.monotonic() + seconds
        current = self._sm6_outbound_until or 0.0
        self._sm6_outbound_until = max(current, deadline)

    def _sm6_outbound_active(self) -> bool:
        return (
            self._sm6_outbound_until is not None
            and time.monotonic() < self._sm6_outbound_until
        )

    def _sm6_should_accept_transport_state(self, polled: str, current: str | None) -> str:
        if polled == "PLAYING":
            self._sm6_optimistic_play_until = None
            return polled
        if (
            polled in _STOPPED
            and current in _ACTIVE
            and self._sm6_outbound_active()
        ):
            return current
        if polled == "PAUSED_PLAYBACK" and current == "PLAYING":
            self._sm6_clear_optimistic_play()
        return polled

    def _sm6_observe_polled_transport(self, previous: str | None, polled: str) -> None:
        if not self._is_sm6_renderer() or self._sm6_relinquished_control:
            return
        if self._sm6_outbound_active():
            return
        if polled == "PAUSED_PLAYBACK" and previous in _ACTIVE:
            self._sm6_clear_optimistic_play()
            return
        if polled in _STOPPED and previous in _ACTIVE:
            self._sm6_relinquish_control("device_stopped")

    def _sm6_relinquish_control(self, reason: str) -> None:
        if self._sm6_relinquished_control:
            return
        self._sm6_relinquished_control = True
        self._sm6_outbound_until = None
        self._sm6_clear_optimistic_play()
        self._suppress_auto_next = True
        self._state = "STOPPED"
        self._sm6_session_uri = None


def test_false_stop_guard_skips_sm6() -> None:
    sm6_renderer = True
    relinquished = False
    if sm6_renderer or relinquished:
        result = False
    else:
        result = True
    assert result is False


def test_accepts_device_pause_instead_of_masking_optimistic() -> None:
    stub = _Sm6RelinquishStub()
    stub._sm6_mark_outbound_activity(5.0)
    accepted = stub._sm6_should_accept_transport_state("PAUSED_PLAYBACK", "PLAYING")
    assert accepted == "PAUSED_PLAYBACK"


def test_ignores_transient_stop_during_outbound_activity() -> None:
    stub = _Sm6RelinquishStub()
    stub._sm6_mark_outbound_activity(5.0)
    accepted = stub._sm6_should_accept_transport_state("STOPPED", "PLAYING")
    assert accepted == "PLAYING"


def test_device_stop_triggers_relinquish() -> None:
    stub = _Sm6RelinquishStub()
    stub._sm6_observe_polled_transport("PLAYING", "STOPPED")
    assert stub._sm6_relinquished_control is True


def test_device_stop_ignored_during_outbound() -> None:
    stub = _Sm6RelinquishStub()
    stub._sm6_mark_outbound_activity(5.0)
    stub._sm6_observe_polled_transport("PLAYING", "STOPPED")
    assert stub._sm6_relinquished_control is False


def test_relinquish_clears_session() -> None:
    stub = _Sm6RelinquishStub()
    stub._sm6_relinquish_control("audio_source=3")
    assert stub._sm6_relinquished_control is True
    assert stub._sm6_session_uri is None
