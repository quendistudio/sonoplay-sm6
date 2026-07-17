"""SM6 SOAP coordination: transport priority, DLNA polling pause."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, TypeVar

from settings import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class _DeviceLane:
    __slots__ = ("soap_lock", "thread_lock", "transport_depth", "poll_paused_until")

    def __init__(self) -> None:
        self.soap_lock = asyncio.Lock()
        self.thread_lock = threading.Lock()
        self.transport_depth = 0
        self.poll_paused_until = 0.0


_lanes: dict[str, _DeviceLane] = {}
_lanes_guard = threading.Lock()


def _transport_hold_seconds() -> float:
    return getattr(settings, "sm6_transport_hold_seconds", 0.2)


def _lane(device_uuid: str) -> _DeviceLane:
    with _lanes_guard:
        lane = _lanes.get(device_uuid)
        if lane is None:
            lane = _DeviceLane()
            _lanes[device_uuid] = lane
        return lane


def _extend_poll_pause(lane: _DeviceLane, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    with lane.thread_lock:
        lane.poll_paused_until = max(lane.poll_paused_until, deadline)


def poll_should_pause(device_uuid: str) -> bool:
    """True during a transport command or poll-pause window (~200 ms)."""
    lane = _lanes.get(device_uuid)
    if lane is None:
        return False
    now = time.monotonic()
    with lane.thread_lock:
        return lane.transport_depth > 0 or now < lane.poll_paused_until


@asynccontextmanager
async def sm6_transport_command(device_uuid: str):
    """Reserve the SOAP lane and suspend DLNA polling around a transport command."""
    lane = _lane(device_uuid)
    hold = _transport_hold_seconds()
    with lane.thread_lock:
        lane.transport_depth += 1
    _extend_poll_pause(lane, hold)
    try:
        yield
    finally:
        with lane.thread_lock:
            lane.transport_depth = max(0, lane.transport_depth - 1)
        _extend_poll_pause(lane, hold)


async def priority_transport_soap(
    device_uuid: str,
    operation: Callable[[], Awaitable[T]],
) -> T:
    """Run a transport SOAP operation with priority over polling."""
    lane = _lane(device_uuid)
    async with sm6_transport_command(device_uuid):
        async with lane.soap_lock:
            return await operation()
