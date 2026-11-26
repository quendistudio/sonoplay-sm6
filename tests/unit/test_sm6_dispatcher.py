"""Unit tests for SM6 instruction dispatcher."""
from __future__ import annotations

import asyncio

import pytest

from dlna import sm6_dispatcher as module
from dlna.sm6_dispatcher import Sm6InstructionDispatcher


@pytest.fixture(autouse=True)
def _fast_soap(settings_override):
    settings_override(sm6_soap_min_interval_seconds=0.0)


@pytest.mark.asyncio
async def test_transport_preempt_cancels_running_queue_job() -> None:
    dispatcher = Sm6InstructionDispatcher("uuid-preempt", "http://sm6/desc.xml")
    order: list[str] = []
    queue_started = asyncio.Event()

    async def queue_job() -> None:
        order.append("queue-start")
        queue_started.set()
        await asyncio.sleep(3600)

    async def transport_job() -> None:
        order.append("transport")

    queue_task = asyncio.create_task(
        dispatcher.submit_queue(queue_job, label="queue", wait=True)
    )
    await queue_started.wait()
    await dispatcher.submit_transport(transport_job, label="transport", wait=True)
    with pytest.raises(asyncio.CancelledError):
        await queue_task
    assert order == ["queue-start", "transport"]


@pytest.mark.asyncio
async def test_control_lane_not_cancelled_on_transport_preempt() -> None:
    dispatcher = Sm6InstructionDispatcher("uuid-control", "http://sm6/desc.xml")
    order: list[str] = []

    async def control_job() -> None:
        order.append("control")

    async def transport_job() -> None:
        order.append("transport")

    await dispatcher.submit_control(control_job, label="control")
    await dispatcher.submit_transport(transport_job, label="transport")
    assert order == ["control", "transport"]


@pytest.mark.asyncio
async def test_control_priority_over_pending_transport() -> None:
    dispatcher = Sm6InstructionDispatcher("uuid-ctrl-prio", "http://sm6/desc.xml")
    order: list[str] = []
    hold = asyncio.Event()
    first_started = asyncio.Event()

    async def blocker() -> None:
        order.append("block")
        first_started.set()
        await hold.wait()

    async def transport_job() -> None:
        order.append("transport")

    async def control_job() -> None:
        order.append("control")

    block_task = asyncio.create_task(
        dispatcher.submit_control(blocker, label="block", wait=True)
    )
    await first_started.wait()
    transport_task = asyncio.create_task(
        dispatcher.submit_transport(
            transport_job,
            label="transport",
            preempts_queue=False,
            wait=True,
        )
    )
    control_task = asyncio.create_task(
        dispatcher.submit_control(control_job, label="control", wait=True)
    )
    await asyncio.sleep(0.01)
    hold.set()
    await asyncio.gather(block_task, transport_task, control_task)
    assert order == ["block", "control", "transport"]


@pytest.mark.asyncio
async def test_executor_rate_limit_gap() -> None:
    original = module._soap_min_interval_seconds
    module._soap_min_interval_seconds = lambda: 0.15
    try:
        dispatcher = Sm6InstructionDispatcher("uuid-rate", "http://sm6/desc.xml")
        stamps: list[float] = []

        async def job() -> None:
            stamps.append(asyncio.get_running_loop().time())

        await dispatcher.submit_control(job, label="c1")
        await dispatcher.submit_control(job, label="c2")
        assert len(stamps) == 2
        assert stamps[1] - stamps[0] >= 0.14
    finally:
        module._soap_min_interval_seconds = original


@pytest.mark.asyncio
async def test_skip_control_lane_does_not_drain_queue() -> None:
    dispatcher = Sm6InstructionDispatcher("uuid-skip", "http://sm6/desc.xml")
    order: list[str] = []
    hold = asyncio.Event()
    first_started = asyncio.Event()

    async def blocker() -> None:
        order.append("block")
        first_started.set()
        await hold.wait()

    async def transport_job() -> None:
        order.append("transport")

    async def skip_job() -> None:
        order.append("skip")

    async def idle_queue() -> None:
        order.append("queue")

    block_task = asyncio.create_task(
        dispatcher.submit_control(blocker, label="block", wait=True)
    )
    await first_started.wait()
    queue_task = asyncio.create_task(
        dispatcher.submit_queue(idle_queue, label="tail", wait=True)
    )
    transport_task = asyncio.create_task(
        dispatcher.submit_transport(
            transport_job,
            label="transport",
            preempts_queue=False,
            wait=True,
        )
    )
    skip_task = asyncio.create_task(
        dispatcher.submit_control(skip_job, label="KeyPressed SKIP_NEXT", wait=True)
    )
    await asyncio.sleep(0.01)
    hold.set()
    await asyncio.gather(block_task, skip_task, transport_task, queue_task)
    assert order == ["block", "skip", "transport", "queue"]


@pytest.mark.asyncio
async def test_reentrant_nested_poll_inside_control_job() -> None:
    dispatcher = Sm6InstructionDispatcher("uuid-reentrant", "http://sm6/desc.xml")
    order: list[str] = []

    async def nested_read() -> str:
        order.append("read")
        return "ok"

    async def job() -> str:
        order.append("job-start")
        result = await dispatcher.submit_poll(nested_read, label="nested-poll")
        order.append("job-end")
        return result

    assert await dispatcher.submit_control(job, label="nested") == "ok"
    assert order == ["job-start", "read", "job-end"]


@pytest.mark.asyncio
async def test_poll_lane_not_drained_on_generation_bump() -> None:
    dispatcher = Sm6InstructionDispatcher("uuid-poll-keep", "http://sm6/desc.xml")
    hold = asyncio.Event()
    started = asyncio.Event()
    order: list[str] = []

    async def blocker() -> None:
        order.append("block")
        started.set()
        await hold.wait()

    async def poll_job() -> str:
        order.append("poll")
        return "polled"

    async def transport_job() -> None:
        order.append("transport")

    block_task = asyncio.create_task(
        dispatcher.submit_control(blocker, label="block", wait=True)
    )
    await started.wait()
    poll_task = asyncio.create_task(
        dispatcher.submit_poll(poll_job, label="state-poll", wait=True)
    )
    transport_task = asyncio.create_task(
        dispatcher.submit_transport(transport_job, label="session", wait=True)
    )
    await asyncio.sleep(0.01)
    hold.set()
    await asyncio.gather(block_task, transport_task)
    assert await poll_task == "polled"
    assert order == ["block", "transport", "poll"]


@pytest.mark.asyncio
async def test_poll_priority_over_queue() -> None:
    dispatcher = Sm6InstructionDispatcher("uuid-poll-prio", "http://sm6/desc.xml")
    order: list[str] = []
    hold = asyncio.Event()
    started = asyncio.Event()

    async def blocker() -> None:
        order.append("block")
        started.set()
        await hold.wait()

    async def queue_job() -> None:
        order.append("queue")

    async def poll_job() -> None:
        order.append("poll")

    block_task = asyncio.create_task(
        dispatcher.submit_control(blocker, label="block", wait=True)
    )
    await started.wait()
    queue_task = asyncio.create_task(
        dispatcher.submit_queue(queue_job, label="tail", wait=True)
    )
    poll_task = asyncio.create_task(
        dispatcher.submit_poll(poll_job, label="state-poll", wait=True)
    )
    await asyncio.sleep(0.01)
    hold.set()
    await asyncio.gather(block_task, poll_task, queue_task)
    assert order == ["block", "poll", "queue"]


@pytest.mark.asyncio
async def test_drain_pending_queue_items_on_generation_bump() -> None:
    dispatcher = Sm6InstructionDispatcher("uuid-drain", "http://sm6/desc.xml")
    item = module._WorkItem(
        lane="queue",
        run=lambda: asyncio.sleep(0),
        label="pending",
        generation=0,
    )
    await dispatcher._queue_q.put(item)
    dispatcher.bump_generation()
    await item.done.wait()
    assert isinstance(item.error, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_transport_preempt_truncates_running_poll_batch() -> None:
    dispatcher = Sm6InstructionDispatcher("uuid-poll-preempt", "http://sm6/desc.xml")
    order: list[str] = []
    poll_step1 = asyncio.Event()

    async def poll_job() -> None:
        order.append("poll-soap-1")
        poll_step1.set()
        await asyncio.sleep(0.05)
        if dispatcher.poll_preempt_requested():
            order.append("poll-truncated")
            return
        order.append("poll-soap-2")

    async def transport_job() -> None:
        order.append("transport")

    poll_task = asyncio.create_task(
        dispatcher.submit_poll(poll_job, label="state-poll", wait=True)
    )
    await poll_step1.wait()
    transport_task = asyncio.create_task(
        dispatcher.submit_transport(
            transport_job,
            label="play album",
            preempts_queue=True,
            wait=True,
        )
    )
    await asyncio.gather(poll_task, transport_task)
    assert "poll-truncated" in order
    assert "poll-soap-2" not in order
    assert "transport" in order


@pytest.mark.asyncio
async def test_state_poll_coalesce_key_replaces_pending_poll() -> None:
    dispatcher = Sm6InstructionDispatcher("uuid-coalesce", "http://sm6/desc.xml")
    hold = asyncio.Event()
    started = asyncio.Event()
    order: list[str] = []

    async def blocker() -> None:
        order.append("block")
        started.set()
        await hold.wait()

    async def first_poll() -> None:
        order.append("poll-old")

    async def second_poll() -> None:
        order.append("poll-new")

    block_task = asyncio.create_task(
        dispatcher.submit_control(blocker, label="block", wait=True)
    )
    await started.wait()
    old_poll = asyncio.create_task(
        dispatcher.submit_poll(
            first_poll,
            label="state-poll",
            coalesce_key="state-poll",
            wait=True,
        )
    )
    await asyncio.sleep(0)
    new_poll = asyncio.create_task(
        dispatcher.submit_poll(
            second_poll,
            label="state-poll",
            coalesce_key="state-poll",
            wait=True,
        )
    )
    await asyncio.sleep(0.01)
    hold.set()
    with pytest.raises(asyncio.CancelledError):
        await old_poll
    await asyncio.gather(block_task, new_poll)
    assert order == ["block", "poll-new"]
