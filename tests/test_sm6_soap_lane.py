"""Tests SM6 priority SOAP lane."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

sys.modules["settings"] = types.SimpleNamespace(
    settings=types.SimpleNamespace(sm6_transport_hold_seconds=0.2)
)

spec = importlib.util.spec_from_file_location(
    "dlna.sm6_soap_lane", _root / "dlna" / "sm6_soap_lane.py"
)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

poll_should_pause = module.poll_should_pause
priority_transport_soap = module.priority_transport_soap
sm6_transport_command = module.sm6_transport_command


async def _poll_paused_during_transport() -> None:
    uuid = "test-transport-uuid"
    assert not poll_should_pause(uuid)

    async with sm6_transport_command(uuid):
        assert poll_should_pause(uuid)

    await asyncio.sleep(0.05)
    assert poll_should_pause(uuid)
    await asyncio.sleep(0.25)
    assert not poll_should_pause(uuid)


def test_poll_paused_during_transport() -> None:
    asyncio.run(_poll_paused_during_transport())


async def _priority_transport_serializes() -> None:
    uuid = "test-serialize-uuid"
    order: list[str] = []

    async def first() -> None:
        order.append("start1")
        await asyncio.sleep(0.05)
        order.append("end1")

    async def second() -> None:
        order.append("start2")
        order.append("end2")

    task1 = asyncio.create_task(priority_transport_soap(uuid, first))
    await asyncio.sleep(0.01)
    await priority_transport_soap(uuid, second)
    await task1
    assert order == ["start1", "end1", "start2", "end2"]


def test_priority_transport_serializes() -> None:
    asyncio.run(_priority_transport_serializes())
