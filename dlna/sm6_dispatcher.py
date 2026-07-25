"""SM6 instruction dispatcher: control, session, queue-build, and poll lanes."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from settings import settings

logger = logging.getLogger(__name__)

Lane = Literal["control", "transport", "poll", "queue"]
RunFn = Callable[[], Awaitable[Any]]

# Dequeue priority (highest first). Poll is never drained on generation bump.
_LANE_PRIORITY: tuple[Lane, ...] = ("control", "transport", "poll", "queue")


@dataclass
class _WorkItem:
    lane: Lane
    run: RunFn
    label: str
    generation: int
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: Any = None
    error: BaseException | None = None


def _soap_min_interval_seconds() -> float:
    return float(getattr(settings, "sm6_soap_min_interval_seconds", 0.3))


class _ReentrantSoapLock:
    """asyncio.Lock that allows nested acquire by the same Task (avoids snapshot deadlocks)."""

    __slots__ = ("_lock", "_owner", "_depth")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def __aenter__(self) -> "_ReentrantSoapLock":
        task = asyncio.current_task()
        if self._owner is task:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        task = asyncio.current_task()
        if self._owner is not task:
            return
        self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            self._owner = None
            self._lock.release()


class Sm6InstructionDispatcher:
    """Per-device SOAP scheduler with preemptible queue-build lane.

    Priority: control > transport > poll > queue. Only transport bumps generation
    (drains queue only). Poll is never cancelled by generation. Executor rate-limits
    consecutive SOAP jobs (``sm6_soap_min_interval_seconds``, default 300 ms).
    """

    __slots__ = (
        "device_uuid",
        "description_url",
        "_generation",
        "_control_q",
        "_transport_q",
        "_poll_q",
        "_queue_q",
        "_coalesce_slots",
        "_executor_task",
        "_active_queue_task",
        "_executing_task",
        "_started",
        "_shutdown",
        "soap_lock",
        "_last_soap_monotonic",
    )

    def __init__(self, device_uuid: str, description_url: str) -> None:
        self.device_uuid = device_uuid
        self.description_url = description_url
        self._generation = 0
        self._control_q: asyncio.Queue[_WorkItem] = asyncio.Queue()
        self._transport_q: asyncio.Queue[_WorkItem] = asyncio.Queue()
        self._poll_q: asyncio.Queue[_WorkItem] = asyncio.Queue()
        self._queue_q: asyncio.Queue[_WorkItem] = asyncio.Queue()
        self._coalesce_slots: dict[str, _WorkItem] = {}
        self._executor_task: asyncio.Task | None = None
        self._active_queue_task: asyncio.Task | None = None
        self._executing_task: asyncio.Task | None = None
        self._started = False
        self._shutdown = False
        self.soap_lock = _ReentrantSoapLock()
        self._last_soap_monotonic = 0.0

    def _queue_for(self, lane: Lane) -> asyncio.Queue[_WorkItem]:
        return {
            "control": self._control_q,
            "transport": self._transport_q,
            "poll": self._poll_q,
            "queue": self._queue_q,
        }[lane]

    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        self._executor_task = asyncio.create_task(
            self._executor_loop(),
            name=f"sm6-dispatch-{self.device_uuid[:8]}",
        )

    def current_generation(self) -> int:
        return self._generation

    def bump_generation(self) -> int:
        self._generation += 1
        self._drain_queue_lane()
        active = self._active_queue_task
        if active is not None and not active.done():
            active.cancel()
        logger.debug(
            "SM6 dispatcher %s generation -> %s",
            self.device_uuid[:8],
            self._generation,
        )
        return self._generation

    def control_pending(self) -> bool:
        return not self._control_q.empty()

    def _drain_queue_lane(self) -> None:
        while True:
            try:
                stale = self._queue_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            stale.error = asyncio.CancelledError("queue lane drained")
            stale.done.set()
        # Drop coalesce slots that belonged to drained queue items only.
        for key, item in list(self._coalesce_slots.items()):
            if item.lane == "queue":
                self._coalesce_slots.pop(key, None)

    async def submit(
        self,
        run: RunFn,
        *,
        lane: Lane,
        label: str,
        preempts_queue: bool = False,
        coalesce_key: str | None = None,
        wait: bool = True,
    ) -> Any:
        self._ensure_started()
        if preempts_queue:
            self.bump_generation()

        item = _WorkItem(
            lane=lane,
            run=run,
            label=label,
            generation=self._generation,
        )

        if coalesce_key and lane in ("queue", "poll"):
            previous = self._coalesce_slots.get(coalesce_key)
            if previous is not None and not previous.done.is_set():
                previous.error = asyncio.CancelledError(f"coalesced by {label}")
                previous.done.set()
            self._coalesce_slots[coalesce_key] = item

        await self._queue_for(lane).put(item)

        if not wait:
            return None
        await item.done.wait()
        if item.error is not None:
            raise item.error
        return item.result

    async def submit_control(
        self,
        run: RunFn,
        *,
        label: str,
        preempts_queue: bool = False,
        wait: bool = True,
    ) -> Any:
        return await self.submit(
            run,
            lane="control",
            label=label,
            preempts_queue=preempts_queue,
            wait=wait,
        )

    async def submit_transport(
        self,
        run: RunFn,
        *,
        label: str,
        preempts_queue: bool = True,
        wait: bool = True,
    ) -> Any:
        return await self.submit(
            run,
            lane="transport",
            label=label,
            preempts_queue=preempts_queue,
            wait=wait,
        )

    async def submit_poll(
        self,
        run: RunFn,
        *,
        label: str,
        coalesce_key: str | None = None,
        wait: bool = True,
    ) -> Any:
        """State-loop / snapshot reads — never drained by generation bump."""
        if self._executing_task is asyncio.current_task():
            # Nested read inside an executing job (already holds soap_lock).
            return await run()
        return await self.submit(
            run,
            lane="poll",
            label=label,
            coalesce_key=coalesce_key,
            wait=wait,
        )

    async def submit_queue(
        self,
        run: RunFn,
        *,
        label: str,
        coalesce_key: str | None = None,
        wait: bool = True,
    ) -> Any:
        return await self.submit(
            run,
            lane="queue",
            label=label,
            coalesce_key=coalesce_key,
            wait=wait,
        )

    async def run_read(self, run: RunFn, *, label: str = "read") -> Any:
        """SOAP read via poll lane (or inline when nested in executor)."""
        return await self.submit_poll(run, label=label)

    async def _dequeue_next(self) -> _WorkItem:
        while True:
            for lane in _LANE_PRIORITY:
                try:
                    return self._queue_for(lane).get_nowait()
                except asyncio.QueueEmpty:
                    pass

            waiters = {
                lane: asyncio.create_task(self._queue_for(lane).get())
                for lane in _LANE_PRIORITY
            }
            done, pending = await asyncio.wait(
                set(waiters.values()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

            completed: dict[Lane, _WorkItem] = {}
            for lane, task in waiters.items():
                if task in done:
                    completed[lane] = task.result()

            for lane in _LANE_PRIORITY:
                if lane not in completed:
                    continue
                winner = completed.pop(lane)
                for other_lane, item in completed.items():
                    self._queue_for(other_lane).put_nowait(item)
                return winner

    async def _await_rate_limit(self) -> None:
        interval = _soap_min_interval_seconds()
        if interval <= 0 or self._last_soap_monotonic <= 0:
            return
        elapsed = time.monotonic() - self._last_soap_monotonic
        remaining = interval - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _execute_item(self, item: _WorkItem) -> None:
        if item.lane == "queue" and item.generation != self._generation:
            logger.debug(
                "SM6 dispatcher skip stale queue job %s gen=%s current=%s",
                item.label,
                item.generation,
                self._generation,
            )
            item.error = asyncio.CancelledError("stale generation")
            item.done.set()
            self._drop_coalesce_slot(item)
            return

        try:
            await self._await_rate_limit()
            logger.debug(
                "SM6 dispatcher run lane=%s label=%s gen=%s",
                item.lane,
                item.label,
                item.generation,
            )
            self._last_soap_monotonic = time.monotonic()
            self._executing_task = asyncio.current_task()
            try:
                async with self.soap_lock:
                    item.result = await item.run()
            finally:
                self._executing_task = None
        except asyncio.CancelledError as exc:
            item.error = exc
        except BaseException as exc:
            logger.warning(
                "SM6 dispatcher job failed lane=%s label=%s: %s",
                item.lane,
                item.label,
                exc,
            )
            item.error = exc
        finally:
            item.done.set()
            self._drop_coalesce_slot(item)

    def _drop_coalesce_slot(self, item: _WorkItem) -> None:
        for key, coalesced in list(self._coalesce_slots.items()):
            if coalesced is item:
                self._coalesce_slots.pop(key, None)

    async def _executor_loop(self) -> None:
        try:
            while not self._shutdown:
                item = await self._dequeue_next()
                exec_task = asyncio.create_task(
                    self._execute_item(item),
                    name=f"sm6-{item.lane}-{item.label}",
                )
                if item.lane == "queue":
                    self._active_queue_task = exec_task
                try:
                    await exec_task
                except asyncio.CancelledError:
                    pass
                finally:
                    if self._active_queue_task is exec_task:
                        self._active_queue_task = None
        except asyncio.CancelledError:
            return


_DISPATCHERS: dict[str, Sm6InstructionDispatcher] = {}


def get_sm6_dispatcher(device_uuid: str, description_url: str) -> Sm6InstructionDispatcher:
    dispatcher = _DISPATCHERS.get(device_uuid)
    if dispatcher is None:
        dispatcher = Sm6InstructionDispatcher(device_uuid, description_url)
        _DISPATCHERS[device_uuid] = dispatcher
    elif description_url and dispatcher.description_url != description_url:
        dispatcher.description_url = description_url
    return dispatcher
