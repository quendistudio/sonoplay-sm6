# SPDX-License-Identifier: GPL-3.0-or-later
#
# Original work Copyright (C) 2021 songchenwen
# Modified work Copyright (C) 2025 plexdlnaplayer-enhanced contributors
#
# This file is part of plexdlnaplayer-enhanced, a fork of plexdlnaplayer.
# Original project: https://github.com/songchenwen/plexdlnaplayer
#
# Modifications from original:
#   - Added virtual device support and fan-out orchestration
#   - Enhanced state management with operation-in-progress protection
#   - Added transport operation debouncing for Sonos compatibility
#   - Improved error handling and timeout management
#   - Added timezone-aware datetime handling
#   - Extended metadata extraction for enhanced UI
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, datetime, timezone
import logging
import math
import random
import time
from threading import Thread, current_thread
import weakref
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)


def _virtual_member_uuids(dlna) -> list[str] | None:
    """Virtual group member UUIDs, or None for a physical device.

    Do not use getattr(dlna, \"member_uuids\") on DlnaDevice: __getattr__
    fabricates a SOAP wrapper for any unknown name.
    """
    from dlna.virtual.devices import VirtualDlnaDevice

    if isinstance(dlna, VirtualDlnaDevice):
        return list(dlna.member_uuids)
    return None


import aiohttp
from dotmap import DotMap
from starlette.datastructures import QueryParams

from plex.play_queue import PlayQueue
from utils import parse_timedelta, convert_volume, g, pms_header, extract_value
from settings import settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from dlna.virtual.devices import VirtualDlnaDevice

adapters = {}
_adapters_lock = asyncio.Lock()

# GetPositionInfo only makes sense during active playback (like async_upnp_client / plexupnp).
_ACTIVE_TRANSPORT_STATES = frozenset({"PLAYING", "PAUSED_PLAYBACK"})
_STOPPED_TRANSPORT_STATES = frozenset({"STOPPED", "NO_MEDIA_PRESENT"})

# Stats persistence does a JSON read-modify-write with fsync; running it on
# the event loop stalls every request during playback transitions. A single
# worker keeps writes ordered and serialized.
_stats_io_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stats-io")


def _persist_stats(fn, *args):
    """Run a blocking settings write off the event loop (ordered)."""
    _stats_io_executor.submit(fn, *args)


async def adapter_by_device(device, query_params: QueryParams = None):
    """Get or create adapter for device with thread-safe access."""
    async with _adapters_lock:
        a = adapters.get(device.uuid, None)
        if a is None:
            a = PlexDlnaAdapter(device, query_params)
            adapters[device.uuid] = a
            asyncio.create_task(a._sm6_sync_on_connect())
        elif query_params is not None:
            a.plex_lib.update(query_params)
        return a


async def remove_adapter(adapter):
    """Remove an adapter and clean up its resources."""
    async with _adapters_lock:
        if adapter.dlna.uuid in adapters:
            # Cancel the Plex.tv registration task
            plex_tv_task = getattr(adapter, '_plex_tv_task', None)
            if plex_tv_task is not None and not plex_tv_task.done():
                plex_tv_task.cancel()
            # Shutdown the state thread before removing
            if hasattr(adapter, 'state') and adapter.state is not None:
                adapter.state.shutdown()
            del adapters[adapter.dlna.uuid]


class PlexLib(object):

    def __init__(self):
        self.protocol = ''
        self.address = ''
        self.port = ''
        self.token = ''
        self.machine_id = ''
        self.client_identifier = ''
        self.device = None
        self._music_library_key: str | None = None

    async def ensure_music_library_key(self) -> str | None:
        """Music library section key: env → cache → PMS /library/sections."""
        if settings.plex_music_library_key:
            return settings.plex_music_library_key
        if self._music_library_key:
            return self._music_library_key
        from plex.runtime_cache import cached_music_library_key, remember_music_library_key

        cached = cached_music_library_key()
        if cached:
            self._music_library_key = cached
            return cached
        if not (self.protocol and self.address and self.port and self.token):
            return None
        try:
            url = self.build_url("/library/sections")
            async with g.http.get(url, headers=self.request_headers(accept_json=True)) as res:
                res.raise_for_status()
                payload = await res.json()
            for section in (payload.get("MediaContainer") or {}).get("Directory") or []:
                if str(section.get("type", "")).lower() == "artist":
                    key = str(section.get("key", "") or "").strip("/").split("/")[-1]
                    if key:
                        self._music_library_key = key
                        remember_music_library_key(key)
                        logger.info("Plex music library section discovered: %s", key)
                        return key
        except Exception as exc:
            logger.debug("Plex music library section discovery failed: %s", exc)
        return None

    def build_url(self, resource, token=True):
        url = f"{self.protocol}://{self.address}:{self.port}{resource}"
        if token:
            if "?" in resource:
                url += f"&X-Plex-Token={self.token}"
            else:
                url += f"?X-Plex-Token={self.token}"
        return url

    def update(self, query: QueryParams):
        if query is None:
            return

        def _q(key: str) -> str | None:
            val = query.get(key)
            if val is None or val == "":
                return None
            return val

        if (protocol := _q("protocol")) is not None:
            self.protocol = protocol
        if (address := _q("address")) is not None:
            self.address = address
        if (port := _q("port")) is not None:
            self.port = int(port)
        if (token := _q("token")) is not None:
            self.token = token
        if (machine_id := _q("machineIdentifier")) is not None:
            self.machine_id = machine_id
        if (client_id := _q("clientIdentifier")) is not None:
            self.client_identifier = client_id
        if self.address and self.port:
            from plex.runtime_cache import remember_plex_session

            remember_plex_session(
                protocol=self.protocol or "http",
                address=self.address,
                port=int(self.port),
                machine_id=self.machine_id or None,
            )

    def request_headers(self, *, accept_json: bool = False) -> dict:
        headers = {}
        if accept_json:
            headers["Accept"] = "application/json"
        client_identifier = self.client_identifier or getattr(self.device, "uuid", None) or self.machine_id
        if client_identifier:
            headers["X-Plex-Client-Identifier"] = client_identifier
        product = getattr(settings, "product", None)
        if product:
            headers.setdefault("X-Plex-Product", product)
        version = getattr(settings, "version", None)
        if version:
            headers.setdefault("X-Plex-Version", version)
        platform = getattr(settings, "platform", None)
        if platform:
            headers.setdefault("X-Plex-Platform", platform)
        platform_version = getattr(settings, "platform_version", None)
        if platform_version:
            headers.setdefault("X-Plex-Platform-Version", platform_version)
        device_name = getattr(self.device, "name", None) or getattr(settings, "client_device_name", None) or product
        if device_name:
            headers.setdefault("X-Plex-Device-Name", device_name)
        descriptor = getattr(settings, "client_model", None) or getattr(self.device, "model", None) or getattr(self.device, "name", None) or product
        device_header = getattr(settings, "client_device", None) or descriptor
        if device_header:
            headers.setdefault("X-Plex-Device", device_header)
        if descriptor:
            headers.setdefault("X-Plex-Model", descriptor)
        client_profile = getattr(settings, "client_profile", None)
        if client_profile:
            headers.setdefault("X-Plex-Client-Profile-Name", client_profile)
        headers.setdefault("X-Plex-Provides", "player,pubsub-player")
        return headers

    def get_info(self):
        return dict(protocol=self.protocol,
                    address=self.address,
                    port=self.port,
                    machineIdentifier=self.machine_id)

    def get_queue(self, container_key):
        return PlayQueue(container_key, self)

    def get_timeline(self):
        return self.build_url("/:/timeline", token=False)

    async def fetch_metadata(self, key: str):
        """Load Plex metadata for a /library/metadata/{id} key."""
        rating_key = key.rstrip("/").rsplit("/", 1)[-1]
        if not rating_key.isdigit():
            return None
        url = self.build_url(
            f"/library/metadata/{rating_key}?includeMedia=1&includeStreamDetails=1"
        )
        async with g.http.get(url, headers=self.request_headers(accept_json=True)) as res:
            res.raise_for_status()
            payload = await res.json()
        container = payload.get("MediaContainer") or {}
        metadata = container.get("Metadata") or []
        if not metadata:
            return None
        return DotMap(metadata[0])

    async def fetch_album_tracks(self, key: str):
        """Album child tracks with full Media info (bitrate, sample rate)."""
        rating_key = key.rstrip("/").rsplit("/", 1)[-1]
        if not rating_key.isdigit():
            return []
        url = self.build_url(
            f"/library/metadata/{rating_key}/children?includeMedia=1&includeStreamDetails=1"
        )
        async with g.http.get(url, headers=self.request_headers(accept_json=True)) as res:
            res.raise_for_status()
            payload = await res.json()
        container = payload.get("MediaContainer") or {}
        return [DotMap(track) for track in container.get("Metadata") or []]

    async def search_track(
        self,
        title: str,
        *,
        artist: str | None = None,
        album: str | None = None,
        album_hint: str | None = None,
    ):
        """Search the Plex library for a track (SM6 queue sync)."""
        from urllib.parse import quote

        title_cf = str(title).casefold()
        artist_cf = str(artist).casefold() if artist else None
        album_cf = str(album).casefold() if album else None
        hint_cf = str(album_hint).casefold() if album_hint else None

        queries: list[str] = []
        if artist and album:
            queries.append(f"{title} {artist} {album}")
        if artist:
            queries.append(f"{title} {artist}")
        if album:
            queries.append(f"{title} {album}")
        queries.append(str(title))

        seen_queries: set[str] = set()
        candidates: list[dict] = []
        seen_keys: set[str] = set()
        section = await self.ensure_music_library_key()

        for query in queries:
            q_key = query.casefold()
            if q_key in seen_queries:
                continue
            seen_queries.add(q_key)

            params = [f"query={quote(query)}", "limit=12", "type=10"]
            if section:
                params.append(f"section={quote(str(section))}")
            url = self.build_url(f"/search?{'&'.join(params)}")
            async with g.http.get(url, headers=self.request_headers(accept_json=True)) as res:
                res.raise_for_status()
                payload = await res.json()

            for hit in (payload.get("MediaContainer") or {}).get("Metadata") or []:
                if str(hit.get("type", "")).lower() != "track":
                    continue
                if str(hit.get("title", "")).casefold() != title_cf:
                    continue
                if artist_cf and str(hit.get("grandparentTitle", "")).casefold() != artist_cf:
                    continue
                if album_cf and str(hit.get("parentTitle", "")).casefold() != album_cf:
                    continue
                rk = str(hit.get("ratingKey", "") or "")
                if rk and rk not in seen_keys:
                    seen_keys.add(rk)
                    candidates.append(hit)

            if candidates and (artist_cf or album_cf):
                break

        if not candidates:
            if artist and album:
                return await self._find_track_on_album(title, artist, album)
            return None
        if len(candidates) == 1:
            return DotMap(candidates[0])

        if hint_cf:
            hinted = [
                h for h in candidates
                if str(h.get("parentTitle", "")).casefold() == hint_cf
            ]
            if len(hinted) == 1:
                return DotMap(hinted[0])

        filtered = candidates
        if artist_cf:
            filtered = [
                h for h in filtered
                if str(h.get("grandparentTitle", "")).casefold() == artist_cf
            ]
        if album_cf:
            filtered = [
                h for h in filtered
                if str(h.get("parentTitle", "")).casefold() == album_cf
            ]
        if len(filtered) == 1:
            return DotMap(filtered[0])

        # ponytail: titre seul + homonymes → pas de devinette
        if not artist_cf and not album_cf:
            return None
        if artist and album:
            return await self._find_track_on_album(title, artist, album)
        return None

    async def _find_track_on_album(self, title: str, artist: str, album: str):
        """Resolve a track by listing album children (short titles like « yes »)."""
        from urllib.parse import quote

        title_cf = str(title).casefold()
        artist_cf = str(artist).casefold()
        album_cf = str(album).casefold()
        params = [f"query={quote(f'{artist} {album}')}", "limit=8", "type=9"]
        section = await self.ensure_music_library_key()
        if section:
            params.append(f"section={quote(str(section))}")
        url = self.build_url(f"/search?{'&'.join(params)}")
        async with g.http.get(url, headers=self.request_headers(accept_json=True)) as res:
            res.raise_for_status()
            payload = await res.json()
        albums = (payload.get("MediaContainer") or {}).get("Metadata") or []
        album_key = None
        for hit in albums:
            if str(hit.get("title", "")).casefold() != album_cf:
                continue
            parent = str(hit.get("parentTitle", "")).casefold()
            grandparent = str(hit.get("grandparentTitle", "")).casefold()
            if parent == artist_cf or grandparent == artist_cf:
                album_key = hit.get("ratingKey")
                break
        if not album_key and len(albums) == 1:
            album_key = albums[0].get("ratingKey")
        if not album_key:
            return None
        url = self.build_url(f"/library/metadata/{album_key}/children")
        async with g.http.get(url, headers=self.request_headers(accept_json=True)) as res:
            res.raise_for_status()
            payload = await res.json()
        for hit in (payload.get("MediaContainer") or {}).get("Metadata") or []:
            if str(hit.get("title", "")).casefold() == title_cf:
                return DotMap(hit)
        return None


class DlnaState(object):
    changing_attrs = ("state", "volume", "elapsed", "current_uri", "current_track_duration", "muted")

    def __init__(self, adapter, state_change_callback=None):
        self.adapter = adapter
        self.dlna = adapter.dlna
        self._state = None
        self._volume = None
        self._elapsed = 0
        self._current_uri = None
        self._current_track_duration = None
        self._muted = None

        self.looping_thread: Thread = None
        self._thread_should_stop = False
        self.running_loop: asyncio.AbstractEventLoop = None
        self.state_change_callback = state_change_callback
        self._changed_state = None
        self.change_session_lock = None
        self._check_all_next_loop = False
        self.looping_wait_event: asyncio.Event = None
        self.last_access_time = datetime.now(timezone.utc)
        self._elapsed_anchor_ms: int | None = None
        self._elapsed_anchor_mono: float | None = None
        self._elapsed_assume_active: bool = True
        self.start_looping()

    def start_looping(self):
        if self.looping_thread is not None and self.looping_thread.is_alive():
            return
        if self.looping_thread is not None and not self.looping_thread.is_alive():
            try:
                self.looping_thread.join(timeout=0)
            except Exception as e:
                logger.debug("Expected cleanup error joining thread: %s", e)
            logger.info("%s state restarting loop thread", self.dlna)
        self._thread_should_stop = False
        self.running_loop = None
        self.looping_wait_event = None
        self.change_session_lock = None
        logger.info("%s state start looping", self.dlna)
        loop_thread = Thread(target=self.background_loop,
                              name=f"Dlna State Thread {str(self.dlna)}",
                              daemon=True)
        loop_thread.start()
        self.looping_thread = loop_thread

    def background_loop(self):
        # Keep a LOCAL reference to the loop so the finally block can always
        # close it even if _check_loop sets self.running_loop = None before
        # returning (which was causing close() to be called on None, leaking
        # all of the uvloop libuv FDs: epoll, eventfd, and pipe pairs).
        loop = asyncio.new_event_loop()
        self.running_loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._check_loop())
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                if pending:
                    for task in pending:
                        task.cancel()
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            finally:
                self.running_loop = None
                loop.close()

    def begin_change_session(self):
        self._changed_state = DotMap()

    def end_change_session(self):
        s = self._changed_state
        self._changed_state = None
        return s

    def _wakeup_loop(self):
        loop = self.running_loop
        event = self.looping_wait_event
        if loop is None or event is None:
            return
        if loop.is_closed():
            return
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            event.set()
        elif loop.is_running():
            loop.call_soon_threadsafe(event.set)

    @property
    def check_all_next_loop(self):
        return self._check_all_next_loop

    @check_all_next_loop.setter
    def check_all_next_loop(self, value: bool):
        if self._check_all_next_loop == value:
            return
        self._check_all_next_loop = value
        if value:
            self._wakeup_loop()

    def __setattr__(self, key, value):
        if key in DlnaState.changing_attrs:
            old_value = self.__getattr__("_" + key)
            if old_value != value and self._changed_state is not None:
                self._changed_state[key] = value
                self._changed_state.old[key] = old_value
            object.__setattr__(self, "_" + key, value)
        else:
            object.__setattr__(self, key, value)

    def __getattr__(self, item):
        if item in DlnaState.changing_attrs:
            return object.__getattribute__(self, "_" + item)
        return object.__getattribute__(self, item)

    def touch_access_time(self):
        """Explicitly mark that a client is actively using this device's state.
        
        Call this from adapter code when handling user-initiated actions
        (polling, playback control) to keep the state loop running at high frequency.
        """
        self.last_access_time = datetime.now(timezone.utc)
        self._wakeup_loop()

    def _reset_elapsed_anchor(self) -> None:
        self._elapsed_anchor_ms = None
        self._elapsed_anchor_mono = None

    def _arm_elapsed_assume_impl(
        self,
        elapsed_ms: int | None = None,
        *,
        delay_seconds: float | None = None,
    ) -> None:
        """Enable extrapolation with delay compensating SM6 transport latency."""
        if elapsed_ms is not None and elapsed_ms != self._elapsed:
            self.elapsed = elapsed_ms
        delay = (
            delay_seconds
            if delay_seconds is not None
            else settings.sm6_position_assume_play_delay_seconds
        )
        self._elapsed_assume_active = True
        self._elapsed_anchor_ms = self._elapsed
        self._elapsed_anchor_mono = time.monotonic() + delay

    def _disarm_elapsed_assume_impl(self) -> None:
        self._elapsed_assume_active = False
        self._reset_elapsed_anchor()

    def arm_elapsed_assume(
        self,
        elapsed_ms: int | None = None,
        *,
        delay_seconds: float | None = None,
    ) -> None:
        self._schedule_elapsed_assume_command(
            arm=True,
            elapsed_ms=elapsed_ms,
            delay_seconds=delay_seconds,
        )

    def disarm_elapsed_assume(self) -> None:
        self._schedule_elapsed_assume_command(arm=False)

    def _schedule_elapsed_assume_command(
        self,
        *,
        arm: bool,
        elapsed_ms: int | None = None,
        delay_seconds: float | None = None,
    ) -> None:
        loop_obj = self.running_loop
        loop_alive = self.looping_thread is not None and self.looping_thread.is_alive()
        loop_ready = loop_alive and loop_obj is not None and not loop_obj.is_closed()
        if not loop_ready:
            self.begin_change_session()
            if arm:
                self._arm_elapsed_assume_impl(elapsed_ms, delay_seconds=delay_seconds)
            else:
                self._disarm_elapsed_assume_impl()
            changed = self.end_change_session()
            if changed and self.state_change_callback:
                self.state_change_callback(changed)
            return
        if current_thread() == self.looping_thread:
            loop_obj.create_task(
                self._elapsed_assume_in_thread(
                    arm=arm,
                    elapsed_ms=elapsed_ms,
                    delay_seconds=delay_seconds,
                )
            )
        else:
            asyncio.run_coroutine_threadsafe(
                self._elapsed_assume_in_thread(
                    arm=arm,
                    elapsed_ms=elapsed_ms,
                    delay_seconds=delay_seconds,
                ),
                loop_obj,
            )

    async def _elapsed_assume_in_thread(
        self,
        *,
        arm: bool,
        elapsed_ms: int | None = None,
        delay_seconds: float | None = None,
    ) -> None:
        async with self.change_session_lock:
            self.begin_change_session()
            if arm:
                self._arm_elapsed_assume_impl(elapsed_ms, delay_seconds=delay_seconds)
            else:
                self._disarm_elapsed_assume_impl()
            changed = self.end_change_session()
            if changed and self.state_change_callback:
                self.state_change_callback(changed)
        self._wakeup_loop()

    def _sync_elapsed_anchor(self, elapsed_ms: int) -> None:
        self._elapsed_anchor_ms = int(elapsed_ms)
        self._elapsed_anchor_mono = time.monotonic()

    def _resync_elapsed_from_sm6(self, soap_elapsed_ms: int) -> None:
        """Realign to SM6 RelTime (whole seconds) without quantizing extrapolation."""
        soap_elapsed_ms = int(soap_elapsed_ms)
        tolerance = int(settings.sm6_position_resync_back_tolerance_ms)
        if (
            soap_elapsed_ms < self._elapsed
            and self._elapsed - soap_elapsed_ms <= tolerance
        ):
            self._sync_elapsed_anchor(self.live_elapsed_ms())
            return
        if soap_elapsed_ms != self._elapsed:
            self.elapsed = soap_elapsed_ms
        self._sync_elapsed_anchor(soap_elapsed_ms)

    def _computed_elapsed_from_anchor(self) -> int | None:
        if self._elapsed_anchor_ms is None or self._elapsed_anchor_mono is None:
            return None
        if time.monotonic() < self._elapsed_anchor_mono:
            return self._elapsed
        delta_ms = round((time.monotonic() - self._elapsed_anchor_mono) * 1000)
        target = self._elapsed_anchor_ms + delta_ms
        duration = self._current_track_duration
        if duration and target > duration:
            target = duration
        return target

    def live_elapsed_ms(self) -> int:
        """Current position: extrapolated while playing, otherwise stored value."""
        if self._elapsed_assume_active and self.state == "PLAYING":
            computed = self._computed_elapsed_from_anchor()
            if computed is not None:
                return computed
        return self._elapsed

    def _refresh_assumed_elapsed(self) -> None:
        """1:1 ms extrapolation; wake Plex on ms delta without quantizing position."""
        if not self._elapsed_assume_active:
            return
        if self.state != "PLAYING":
            self._disarm_elapsed_assume_impl()
            return
        target = self._computed_elapsed_from_anchor()
        if target is None:
            self._sync_elapsed_anchor(self._elapsed)
            return
        min_delta = int(settings.sm6_position_plex_notify_min_delta_ms)
        delta = target - self._elapsed
        if target == self._elapsed or (delta >= 0 and delta < min_delta):
            return
        self.elapsed = target

    def _apply_assumed_elapsed_tick(self) -> None:
        """Alias kept for existing tests and callers."""
        self._refresh_assumed_elapsed()

    def __del__(self):
        # Only flag the thread to stop — joining here can block the GC.
        # Explicit lifecycle teardown is handled by shutdown().
        self._thread_should_stop = True

    def shutdown(self):
        """Cleanly stop the background polling thread.
        
        This method is idempotent - safe to call multiple times.
        Wakes any sleeping thread to ensure prompt shutdown.
        """
        if self._thread_should_stop:
            return  # Already shutting down
            
        self._thread_should_stop = True
        
        # Wake the thread if it's sleeping
        running_loop = getattr(self, "running_loop", None)
        looping_event = getattr(self, "looping_wait_event", None)
        
        if running_loop is not None and not running_loop.is_closed():
            def _wake():
                if looping_event is not None:
                    looping_event.set()
            try:
                running_loop.call_soon_threadsafe(_wake)
            except RuntimeError:
                # Loop already closed
                pass
        elif looping_event is not None:
            looping_event.set()
        
        # Wait for thread to finish
        if self.looping_thread is not None and self.looping_thread.is_alive():
            self.looping_thread.join(timeout=2.0)

    def __repr__(self):
        return f"{self.dlna.name}: state {self.state} {self.elapsed} {self.volume} " \
               f"{self.muted} {self.current_track_duration} {self.current_uri}"

    async def check(self, client: aiohttp.ClientSession, check_count=0):
        sm6_renderer = hasattr(self, "adapter") and self.adapter._is_sm6_renderer()
        if sm6_renderer and hasattr(self.adapter, "_sm6_any_poll_paused_sync"):
            if self.adapter._sm6_any_poll_paused_sync():
                return

        position_check_count = 1
        if sm6_renderer:
            position_check_count = max(1, settings.sm6_poll_position_every_cycles)
            volume_check_count = max(1, settings.sm6_poll_volume_every_cycles)
            muted_check_count = max(1, settings.sm6_poll_mute_every_cycles)
            state_check_count = max(1, settings.sm6_poll_transport_every_cycles)
        else:
            volume_check_count = 12
            state_check_count = 10
            muted_check_count = 51
        poll_position_cycle = check_count % position_check_count == 0 or self.check_all_next_loop
        poll_transport = (
            poll_position_cycle
            or check_count % state_check_count == 0
            or self.state == "TRANSITIONING"
            or self.check_all_next_loop
        )
        poll_volume = check_count % volume_check_count == 0 or self.check_all_next_loop
        if poll_volume and sm6_renderer and hasattr(self.adapter, "_sm6_should_skip_volume_poll"):
            if await self.adapter._sm6_should_skip_volume_poll():
                poll_volume = False
        poll_muted = check_count % muted_check_count == 0 or self.check_all_next_loop
        force_poll = self.check_all_next_loop
        if self.check_all_next_loop:
            self.check_all_next_loop = False

        transport_info = None
        position_info = None
        volume_info = None
        muted_info = None
        parallel: list = []
        if poll_transport:
            parallel.append(("transport", self.dlna.GetTransportInfo(client=client)))
        if poll_volume:
            parallel.append(("volume", self.dlna.GetVolume(client=client)))
        if poll_muted:
            parallel.append(("mute", self.dlna.GetMute(client=client)))

        if parallel:
            results = await asyncio.gather(*(coro for _, coro in parallel), return_exceptions=True)
            for (label, _), result in zip(parallel, results):
                if isinstance(result, BaseException):
                    if __debug__:
                        logger.debug("dlna %s state loop %s error: %s", self.dlna.name, label, result)
                    continue
                if label == "transport":
                    transport_info = result
                elif label == "volume":
                    volume_info = result
                elif label == "mute":
                    muted_info = result

        self.begin_change_session()

        if transport_info is not None:
            polled_state = transport_info.CurrentTransportState
            previous_state = self.state
            if sm6_renderer and hasattr(self.adapter, "_sm6_observe_polled_transport"):
                self.adapter._sm6_observe_polled_transport(previous_state, polled_state)
            if sm6_renderer and hasattr(self.adapter, "_sm6_should_accept_transport_state"):
                polled_state = self.adapter._sm6_should_accept_transport_state(
                    polled_state,
                    previous_state,
                )
            self.state = polled_state

        if poll_position_cycle:
            if self.state in _ACTIVE_TRANSPORT_STATES:
                sync_playlist = (
                    sm6_renderer
                    and hasattr(self.adapter, "_sm6_maybe_sync_playlist")
                    and (
                        force_poll
                        or check_count % max(1, settings.sm6_poll_playlist_every_cycles) == 0
                    )
                )
                if sync_playlist and not self.adapter._sm6_plex_play_in_progress:
                    asyncio.run_coroutine_threadsafe(
                        self.adapter._sm6_maybe_sync_playlist(),
                        self.adapter.loop,
                    )
                try:
                    position_info = await self.dlna.GetPositionInfo(client=client)
                except Exception as exc:
                    if __debug__:
                        logger.debug("dlna %s GetPositionInfo error: %s", self.dlna.name, exc)
                    position_info = None
                if position_info is not None:
                    previous_uri = self.current_uri
                    new_elapsed = int(parse_timedelta(position_info.RelTime).total_seconds() * 1000)
                    if sm6_renderer:
                        self._resync_elapsed_from_sm6(new_elapsed)
                    else:
                        self.elapsed = new_elapsed
                    track_uri = position_info.TrackURI
                    if hasattr(self.adapter, "_sm6_effective_track_uri"):
                        track_uri = self.adapter._sm6_effective_track_uri(track_uri)
                    self.current_uri = track_uri
                    self.current_track_duration = int(
                        parse_timedelta(position_info.TrackDuration).total_seconds() * 1000)
                    if (
                        track_uri
                        and track_uri != previous_uri
                        and hasattr(self.adapter, "_sm6_sync_from_polled_uri")
                    ):
                        asyncio.run_coroutine_threadsafe(
                            self.adapter._sm6_sync_from_polled_uri(track_uri),
                            self.adapter.loop,
                        )
                    if (
                        self.state in _ACTIVE_TRANSPORT_STATES
                        and hasattr(self.adapter, "_sm6_poll_shuffle_repeat")
                        and check_count % 20 == 0
                    ):
                        asyncio.run_coroutine_threadsafe(
                            self.adapter._sm6_poll_shuffle_repeat(),
                            self.adapter.loop,
                        )
            elif self.state in _STOPPED_TRANSPORT_STATES or self.state is None:
                self.current_uri = None
                self.current_track_duration = None
                self.elapsed = 0
                if sm6_renderer:
                    self._disarm_elapsed_assume_impl()

        if sm6_renderer:
            if (
                hasattr(self.adapter, "_sm6_poll_audio_source")
                and check_count % 10 == 0
            ):
                asyncio.run_coroutine_threadsafe(
                    self.adapter._sm6_poll_audio_source(),
                    self.adapter.loop,
                )
            if self.state == "PLAYING":
                if not poll_position_cycle or position_info is None:
                    self._refresh_assumed_elapsed()
            elif self.state != "PLAYING":
                self._disarm_elapsed_assume_impl()

        if volume_info is not None:
            volume_value = extract_value(getattr(volume_info, 'CurrentVolume', None))
            if hasattr(self, "adapter") and self.adapter._is_sm6_renderer():
                from dlna.sm6_volume import dlna_level_to_plex

                volume_range = await self.adapter._sm6_volume_range()
                plex_vol = dlna_level_to_plex(volume_value, volume_range)
                logger.debug(
                    "%s GetVolume raw=%r range=%s..%s -> plex=%s%%",
                    self.dlna.name,
                    volume_value,
                    volume_range.minimum,
                    volume_range.maximum,
                    plex_vol,
                )
                self.volume = plex_vol
            else:
                try:
                    volume_value = int(volume_value)
                except (TypeError, ValueError):
                    volume_value = self.dlna.volume_min
                self.volume = convert_volume(
                    volume_value, self.dlna.volume_max, self.dlna.volume_min, 100, 0, 1
                )
        if muted_info is not None:
            muted_value = extract_value(getattr(muted_info, 'CurrentMute', None))
            if isinstance(muted_value, str):
                muted_value = muted_value.lower() in ("1", "true", "yes")
            self.muted = bool(muted_value)

        changed_state = self.end_change_session()
        if changed_state and self.state_change_callback:
            self.state_change_callback(changed_state)

    @property
    def loop_interval(self):
        if self.state in ("PLAYING", "TRANSITIONING"):
            if hasattr(self.adapter, "_is_sm6_renderer") and self.adapter._is_sm6_renderer():
                return settings.sm6_poll_playing_interval_seconds
            return 0.8
        if datetime.now(timezone.utc) - self.last_access_time >= timedelta(seconds=90):
            return settings.adapter_idle_interval
        if self.state in ("STOPPED", "NO_MEDIA_PRESENT", None):
            # Watched but idle: no position to track, relax the SOAP chatter.
            # Commands wake the loop immediately via looping_wait_event.
            return 5.0
        return 0.8

    async def wait_for_next_loop(self):
        try:
            await asyncio.wait_for(self.looping_wait_event.wait(), timeout=self.loop_interval)
        except asyncio.TimeoutError:
            pass  # Expected timeout for loop interval
        except Exception as e:
            logger.debug("Expected cleanup error during loop wait: %s", e)
        self.looping_wait_event.clear()

    async def _check_loop(self):
        logger.debug("state loop %s begin in %s", self.dlna.name, current_thread().name)
        if self.change_session_lock is None:
            self.change_session_lock = asyncio.Lock()
        if self.looping_wait_event is None:
            self.looping_wait_event = asyncio.Event()
        async with aiohttp.ClientSession() as client:
            check_count = 0
            one_batch_count = 500
            while not self._thread_should_stop:
                async with self.change_session_lock:
                    await self.check(client, check_count=check_count)
                check_count += 1
                if check_count > one_batch_count:
                    check_count = 0
                await self.wait_for_next_loop()
        logger.debug("%s state loop stopped", self.dlna.name)

    def update(self, state: str = "", uri: str = "", position: str = ""):
        elapsed = ""
        if position:
            elapsed = int(parse_timedelta(position).total_seconds() * 1000)
        if (state == "" or self.state == state) and (uri == "" or self.current_uri == uri) and (elapsed == "" or self.elapsed == elapsed):
            return
        loop_obj = self.running_loop
        loop_alive = self.looping_thread is not None and self.looping_thread.is_alive()
        loop_ready = loop_alive and loop_obj is not None and not loop_obj.is_closed()
        if not loop_ready:
            self.start_looping()
            self._apply_update_without_loop(state=state, uri=uri, elapsed=elapsed)
            return
        if current_thread() == self.looping_thread:
            loop_obj.create_task(self.update_in_thread(state=state, uri=uri, elapsed=elapsed))
        else:
            asyncio.run_coroutine_threadsafe(self.update_in_thread(state=state, uri=uri, elapsed=elapsed),
                                             loop_obj)

    async def update_in_thread(self, state="", uri="", elapsed=""):
        if state == "":
            state = self.state
        if uri == "":
            uri = self.current_uri
        if elapsed == "":
            elapsed = self.elapsed
        if self.state == state and self.current_uri == uri and self.elapsed == elapsed:
            return
        if __debug__:
            logger.debug("%s real update state from sub %s %s %s", self.dlna.name, state, uri, elapsed)
        async with self.change_session_lock:
            if __debug__:
                logger.debug("%s real update state from sub in lock %s %s %s", self.dlna.name, state, uri, elapsed)
            self.begin_change_session()
            self.state = state
            self.current_uri = uri
            self.elapsed = elapsed
            changed = self.end_change_session()
            if changed and self.state_change_callback:
                self.state_change_callback(changed)

    def _apply_update_without_loop(self, state: str = "", uri: str = "", elapsed="") -> None:
        if state == "":
            state = self.state
        if uri == "":
            uri = self.current_uri
        if elapsed == "":
            elapsed = self.elapsed
        if self.state == state and self.current_uri == uri and self.elapsed == elapsed:
            return
        if __debug__:
            logger.debug("%s applying state update without loop %s %s %s", self.dlna.name, state, uri, elapsed)
        self.begin_change_session()
        self.state = state
        self.current_uri = uri
        self.elapsed = elapsed
        changed = self.end_change_session()
        if changed and self.state_change_callback:
            self.state_change_callback(changed)


class PlexDlnaAdapter(object):

    def __init__(self, dlna, query: QueryParams = None):
        logger.info("init adapter for %s in thread %s", dlna, current_thread().name)
        self.dlna = dlna
        self.plex_lib = PlexLib()
        self.plex_lib.device = self.dlna
        if query is not None:
            self.plex_lib.update(query)
        if not self.plex_lib.client_identifier:
            self.plex_lib.client_identifier = self.dlna.uuid
        self.queue = None
        self.state: DlnaState = DlnaState(self, self.state_changed_callback)
        self.shuffle = 0
        self.repeat = 0
        self.plex_bind_token = settings.get_token_for_uuid(self.dlna.uuid)
        self.no_notice = False
        self.loop = asyncio.get_running_loop()
        self.wait_state_change_events = []
        self.delay_stop_state_looping_task: asyncio.Task = None
        self.waiting_sub = 0
        self.current_track_info = None
        self._sm6_session_uri: Optional[str] = None
        self._sm6_last_queue_track_id: Optional[int] = None
        self._sm6_playlist_snapshot = None
        self._sm6_queue_poll_counter = 0
        self._sm6_queue_base_offset = 0
        self._sm6_volume_timeline_task: Optional[asyncio.Task] = None
        self._sm6_play_notify_task: Optional[asyncio.Task] = None
        self._sm6_transcode_prefetch_task: Optional[asyncio.Task] = None
        self._sm6_enqueued_tracks: tuple = ()
        self._sm6_synced_tail_item_ids: tuple[int, ...] = ()
        self._sm6_optimistic_play_until: float | None = None
        self._sm6_outbound_until: float | None = None
        self._sm6_volume_grace_until: float | None = None
        self._sm6_relinquished_control = False
        self._sm6_sonoplay_owned_playback = False
        self._sm6_plex_play_in_progress = False
        self._sm6_last_plex_playlist_fingerprint: tuple | None = None
        self._sm6_playlist_rebuild_backoff_until: float = 0.0
        self._sm6_last_audio_source: int | None = None
        stored_stats = settings.get_device_stats(self.dlna.uuid)
        self.stats_play_count = stored_stats.get('play_count', 0)
        self.stats_play_duration_ms = stored_stats.get('play_duration_ms', 0)
        self.stats_session_start: datetime = None
        # Flag to track if this device is being controlled by a virtual device
        self._controlled_by_virtual_device = False
        self._virtual_controller_ref: Optional[weakref.ReferenceType] = None
        self._transport_lock = asyncio.Lock()
        self._operation_sequence = 0
        self._active_operation_id = 0
        self._active_target_uri: Optional[str] = None
        self._active_operation_event: Optional[asyncio.Event] = None
        self._active_operation_uri_confirmed = False
        self._active_operation_state_ready = False
        self._active_operation_state_confirmed = False
        self._active_operation_target_paused = False
        self._transport_state_override: Optional[dict] = None
        self._transport_settle_timeout = 6.0
        self._transport_max_attempts = 3
        self._suppress_auto_next = False
        self._transport_cancel_requested = False
        self._last_operation_finish_time: Optional[float] = None
        self._post_operation_protection_window = 2.0  # seconds
        self._last_finished_target_uri: Optional[str] = None
        self._in_false_stop_recovery = False
        self._auto_next_in_flight = False
        # Premature STOPPED filtering (LMS-uPnP #63, go2tv #43)
        self._seen_playing_since_operation = False
        self._operation_start_time: Optional[float] = None

    async def _sm6_sync_on_connect(self) -> None:
        """Sync Plex state on first contact without an SM6 hardware command."""
        from plex.device_profiles import needs_plex_dlna_stream_url
        try:
            await self.dlna.get_data()
            if not needs_plex_dlna_stream_url(self.dlna):
                return
            logger.info("%s SM6 connect — passive sync (read-only)", self.dlna.name)
            from dlna.sm6_control import Sm6Control

            sm6 = Sm6Control(self.dlna.location_url)
            await sm6.ensure_power_on()
            await self._sm6_refresh_volume_from_device()
            transport = await self.dlna.GetTransportInfo()
            transport_state = getattr(transport, "CurrentTransportState", None)
            if transport_state in _ACTIVE_TRANSPORT_STATES:
                self._sm6_sonoplay_owned_playback = False
                await self._sm6_hydrate_playback_on_connect()
            else:
                self.state.update(state="STOPPED", uri=None)
                self._sm6_session_uri = None
        except Exception as exc:
            logger.warning("%s SM6 connect sync failed: %s", self.dlna.name, exc)

    async def _sm6_hydrate_playback_on_connect(self) -> None:
        """If the SM6 is already playing, expose track, queue, and position to Plex on connect."""
        try:
            transport = await self.dlna.GetTransportInfo()
            transport_state = getattr(transport, "CurrentTransportState", None)
            if transport_state not in _ACTIVE_TRANSPORT_STATES:
                return
            from dlna.sm6_control import Sm6Control

            sm6 = Sm6Control(self.dlna.location_url)
            playlist_state = await sm6.read_playlist_state(fetch_tracks=True)
            self._sm6_playlist_snapshot = playlist_state
            self._sm6_last_queue_track_id = playlist_state.current_track_id

            position = await self.dlna.GetPositionInfo()
            rel_time = extract_value(getattr(position, "RelTime", None), "0")
            uri = extract_value(getattr(position, "TrackURI", None), "")

            self._sm6_sonoplay_owned_playback = False
            if uri and self._sm6_uri_is_plex_resolvable(uri):
                if await self._ensure_plex_lib_for_sm6_api():
                    await self._sm6_sync_from_polled_uri(uri)
                if self.current_track_info or await self._sm6_is_playlist_queue_coherent(
                    playlist_state
                ):
                    if not self.current_track_info:
                        await self._sm6_apply_queue_track(
                            playlist_state.current_track_id,
                            queue_index=playlist_state.media_queue_index,
                        )
                    self.state.update(
                        state=transport_state,
                        uri=self._sm6_session_uri or uri,
                        position=rel_time,
                    )
                    self._sm6_wake_waiters()
                    return

            if await self._sm6_is_playlist_queue_coherent(playlist_state):
                await self._sm6_apply_queue_track(
                    playlist_state.current_track_id,
                    queue_index=playlist_state.media_queue_index,
                )
                self.state.update(
                    state=transport_state,
                    uri=self._sm6_session_uri or uri,
                    position=rel_time,
                )
                self._sm6_wake_waiters()
                return

            if await self._sm6_rebuild_plex_playlist_from_sm6(force=True):
                self.state.update(
                    state=transport_state,
                    uri=self._sm6_session_uri or uri,
                    position=rel_time,
                )
                self._sm6_wake_waiters()
                return

            if playlist_state.length > 0 and playlist_state.current_track_id >= 0:
                entry = self._sm6_entry_for_track_id(
                    playlist_state.current_track_id,
                    playlist_state=playlist_state,
                )
                if entry is not None:
                    track = await self._sm6_resolve_entry_to_track(
                        entry,
                        queue_index=playlist_state.media_queue_index,
                    )
                    if track is not None:
                        if self.queue is not None and playlist_state.media_queue_index >= 0:
                            selected = await self.queue.selected_offset()
                            self._sm6_queue_base_offset = max(
                                0, selected - playlist_state.media_queue_index
                            )
                        self.current_track_info = track
                        from plex.url_resolver import get_url_resolver
                        try:
                            self._sm6_session_uri = await get_url_resolver().resolve_stream_url(track)
                        except LookupError:
                            self._sm6_session_uri = uri or None
                        self.state.update(
                            state=transport_state,
                            uri=self._sm6_session_uri or uri,
                            position=rel_time,
                        )
                        logger.info(
                            "%s SM6 hydrate connect (playlist): %s (ratingKey=%s, track_id=%s, index=%s) @ %s",
                            self.dlna.name,
                            getattr(track, "title", "?"),
                            getattr(track, "ratingKey", "?"),
                            playlist_state.current_track_id,
                            playlist_state.media_queue_index,
                            rel_time,
                        )
                        self._sm6_sonoplay_owned_playback = False
                        self._sm6_wake_waiters()
                        return

            if not uri:
                return
            if not self._sm6_uri_is_plex_resolvable(uri):
                logger.info(
                    "%s SM6 playing but URI not Plex/DLNA resolvable: %s",
                    self.dlna.name,
                    uri,
                )
                return
            from plex.url_resolver import get_url_resolver
            rating_key = get_url_resolver().rating_key_for_stream_url(uri)
            if not rating_key:
                logger.info(
                    "%s SM6 playing but URI %s not resolved (DLNA cache)",
                    self.dlna.name,
                    uri,
                )
                return
            metadata = await self.plex_lib.fetch_metadata(f"/library/metadata/{rating_key}")
            if metadata is None:
                return
            self.current_track_info = metadata
            self._sm6_session_uri = uri
            self.state.update(
                state=transport_state,
                uri=uri,
                position=rel_time,
            )
            logger.info(
                "%s SM6 hydrate connect (uri): %s (ratingKey=%s) @ %s",
                self.dlna.name,
                getattr(metadata, "title", "?"),
                rating_key,
                rel_time,
            )
            self._sm6_sonoplay_owned_playback = False
            self._sm6_wake_waiters()
        except Exception as exc:
            logger.debug("%s SM6 hydrate on connect failed: %s", self.dlna.name, exc)

    async def _with_no_notice(self, coro):
        """Wrap a coroutine so no_notice is True while it runs on the main loop."""
        self.no_notice = True
        try:
            await coro
        finally:
            self.no_notice = False
            self._auto_next_in_flight = False

    def _start_transport_operation(self, target_uri: str) -> int:
        self._operation_sequence += 1
        self._active_operation_id = self._operation_sequence
        self._active_target_uri = target_uri
        self._active_operation_event = asyncio.Event()
        self._active_operation_uri_confirmed = False
        self._active_operation_state_ready = False
        self._active_operation_state_confirmed = False
        self._active_operation_target_paused = False
        self._transport_state_override = {
            "state": "TRANSITIONING",
            "current_uri": target_uri
        }
        self._suppress_auto_next = False
        # Reset premature STOPPED tracking
        self._seen_playing_since_operation = False
        self._operation_start_time = time.monotonic()
        logger.debug("%s transport operation %d started for %s", self.dlna.name, self._active_operation_id, target_uri)
        return self._active_operation_id

    def _finish_transport_operation(self, operation_id: int) -> None:
        if self._active_operation_id != operation_id:
            return
        logger.debug("%s transport operation %d finished", self.dlna.name, operation_id)
        # Record finish time and URI for post-operation protection
        self._last_operation_finish_time = time.monotonic()
        self._last_finished_target_uri = self._active_target_uri
        # Clear active operation state
        self._active_operation_id = 0
        self._active_target_uri = None
        self._active_operation_event = None
        self._active_operation_uri_confirmed = False
        self._active_operation_state_ready = False
        self._active_operation_state_confirmed = False
        self._active_operation_target_paused = False
        self._transport_state_override = None

    def _should_ignore_premature_stopped(self, changed_state: DotMap) -> bool:
        """Check if STOPPED state should be ignored as premature.
        
        Many devices (Sony, Denon, recent TVs) emit STOPPED immediately after
        SetAVTransportURI but before Play command is sent. This causes the
        event subscription to be closed prematurely.
        
        Source: LMS-uPnP #63, go2tv #43
        
        Returns True if STOPPED should be ignored.
        """
        # Only check during active transport operations
        if self._active_operation_id == 0:
            return False
        
        # Only filter STOPPED state changes
        if 'state' not in changed_state or changed_state.state != "STOPPED":
            return False
        
        # Check quirk setting
        from dlna.quirks import get_device_quirks
        quirks = get_device_quirks(self.dlna)
        if not quirks.get("ignore_premature_stopped", True):
            return False
        
        # If we've seen PLAYING, STOPPED is legitimate
        if self._seen_playing_since_operation:
            return False
        
        # Safety timeout: if operation started >10s ago, accept STOPPED
        if self._operation_start_time:
            elapsed = time.monotonic() - self._operation_start_time
            if elapsed > 10.0:
                logger.debug("%s accepting STOPPED after %.1fs timeout", self.dlna.name, elapsed)
                return False
        
        logger.debug("%s ignoring premature STOPPED (no PLAYING seen yet)", self.dlna.name)
        return True

    def _check_post_operation_false_stop(self, changed_state: DotMap) -> bool:
        """
        Detect and recover from spurious STOPPED state immediately after transport operation.
        
        Sonos devices sometimes report PLAYING briefly, then STOPPED, causing playback
        to get stuck. This method detects when:
        1. State changed from PLAYING to STOPPED
        2. We recently finished a transport operation (within protection window)
        3. The track just started (elapsed is very low)
        
        When detected, it triggers a retry of the current track.
        Returns True if false stop was detected and handled, False otherwise.
        """
        # Sonos-only: SM6 handles transport itself; recovery would hijack source changes.
        if self._is_sm6_renderer() or self._sm6_relinquished_control:
            return False

        # Don't re-trigger while a recovery is already in progress
        if self._in_false_stop_recovery:
            return False
        
        # Only check for PLAYING -> STOPPED transitions
        if 'state' not in changed_state:
            return False
        if changed_state.state != "STOPPED":
            return False
        if changed_state.old.get('state') != "PLAYING":
            return False
        
        # Check if we're within the post-operation protection window
        if self._last_operation_finish_time is None:
            return False
        
        time_since_finish = time.monotonic() - self._last_operation_finish_time
        if time_since_finish > self._post_operation_protection_window:
            return False
        
        # Check if track just started (elapsed is low, indicating false stop not natural end)
        elapsed = self.state.elapsed if hasattr(self.state, 'elapsed') else 0
        duration = self.state.current_track_duration if hasattr(self.state, 'current_track_duration') else 0
        
        # If elapsed is more than 5 seconds and track is substantial, this might be legitimate
        # (though still suspicious if within protection window)
        if elapsed > 5000 and duration > 0 and (duration - elapsed) > 5000:
            return False
        
        logger.info("%s detected false STOP %.2fs after transport operation (elapsed=%dms, duration=%dms), triggering recovery",
                    self.dlna.name, time_since_finish, elapsed, duration)
        
        # Clear the finish time to prevent infinite retry loops
        self._last_operation_finish_time = None
        
        # Suppress auto-next during recovery and guard against re-trigger
        self._suppress_auto_next = True
        self._in_false_stop_recovery = True
        
        # Schedule recovery: replay the current track
        async def recover_playback():
            try:
                if self.queue is not None:
                    logger.info("%s recovery: replaying current track", self.dlna.name)
                    await self.play_selected_queue_item()
                else:
                    logger.info("%s recovery: no queue, cannot replay", self.dlna.name)
            finally:
                self._suppress_auto_next = False
                self._in_false_stop_recovery = False
        
        asyncio.run_coroutine_threadsafe(recover_playback(), self.loop)
        return True

    def attach_virtual_controller(self, controller: "VirtualDlnaDevice") -> None:
        self._virtual_controller_ref = weakref.ref(controller)
        self._controlled_by_virtual_device = True

    def detach_virtual_controller(self, controller: "VirtualDlnaDevice") -> None:
        if self._virtual_controller_ref is None:
            return
        current = self._virtual_controller_ref()
        if current is controller:
            self._virtual_controller_ref = None
            self._controlled_by_virtual_device = False

    def virtual_controller(self) -> Optional["VirtualDlnaDevice"]:
        if self._virtual_controller_ref is None:
            return None
        return self._virtual_controller_ref()

    def check_auto_next(self, changed: DotMap):
        # The SM6 handles track advance itself; software auto-next
        # used stale Plex duration and stopped playback too early.
        if self._is_sm6_renderer():
            return False
        # Skip auto-next logic when a virtual device is orchestrating playback for this member
        if self._controlled_by_virtual_device:
            return False
        if self._suppress_auto_next:
            return False
        if self._auto_next_in_flight:
            return False
        if self._active_operation_id:
            return False
        if self.queue is None:
            return False
        if changed.state and changed.state != "PLAYING" and changed.old.state == "TRANSITIONING":
            return False
        if self._is_sm6_renderer() and (self.shuffle > 0 or self._repeat_value() == 2):
            return False

        async def auto_next():
            if self._repeat_value() == 1:
                await self.play_selected_queue_item()
            elif self._repeat_value() == 2 and \
                    (await self.queue.selected_offset()) >= (await self.queue.total_count() - 1) and \
                    self.shuffle == 0:
                await self.queue.set_selected_offset(0)
                await self.play_selected_queue_item()
            else:
                await self.next()

        if self.state.current_uri is not None and not changed.state and not changed.current_uri and self.current_track_info:
            if (changed.elapsed == 0 < changed.old.elapsed <= self.current_track_info.duration
                and self.current_track_info.duration - changed.old.elapsed <= 2000) \
                    or (
                    changed.elapsed and changed.elapsed > changed.old.elapsed and
                    self.current_track_info.duration // 1000 * 1000 <= changed.elapsed <= self.current_track_info.duration):
                logger.info("auto next stopped %s, elapsed: %s -> %s, %s",
                            self.state.state, changed.old.elapsed, changed.elapsed, self.current_track_info.duration)
                # Set no_notice and auto_next flag IMMEDIATELY (before scheduling)
                # to prevent the subscriber from reporting STOPPED to Plex,
                # which would cause Plex to send a stale stop command that
                # kills the auto-next playback.
                self.no_notice = True
                self._auto_next_in_flight = True
                self.state.update(state="TRANSITIONING", uri=None)
                asyncio.run_coroutine_threadsafe(self._with_no_notice(auto_next()), self.loop)
                return True
        elif not changed.current_uri and changed.old.state == "PLAYING" and changed.state == "STOPPED":
            # _suppress_auto_next guards intentional stops (set by stop()); when the
            # device stops naturally we can't rely on elapsed because Sonos resets it
            # to 0 before we poll it, making the old `<= 1ms` check unreachable.
            logger.info("auto next transitioning %s %s (elapsed=%s duration=%s)",
                        changed.old.state, changed.state, self.state.elapsed, self.state.current_track_duration)
            self.no_notice = True
            self._auto_next_in_flight = True
            self.state.update(state="TRANSITIONING", uri=None)
            asyncio.run_coroutine_threadsafe(self._with_no_notice(auto_next()), self.loop)
            return True
        return False

    def _is_sm6_renderer(self) -> bool:
        from plex.device_profiles import is_sm6_like
        from dlna.dlna_device import devices as physical_devices

        member_uuids = _virtual_member_uuids(self.dlna)
        if member_uuids:
            member_set = set(member_uuids)
            for device in physical_devices:
                if device.uuid in member_set and is_sm6_like(device):
                    return True
            return False
        return is_sm6_like(self.dlna)

    async def _sm6_target_devices(self):
        """Physical SM6 devices to drive (direct or virtual group members)."""
        from dlna.sm6_rendering_control import pick_sm6_control_device
        from plex.device_profiles import is_sm6_like
        from dlna.dlna_device import devices as physical_devices

        member_uuids = _virtual_member_uuids(self.dlna)
        if member_uuids:
            member_set = set(member_uuids)
            candidates = [
                device
                for device in physical_devices
                if device.uuid in member_set and is_sm6_like(device)
            ]
            return pick_sm6_control_device(candidates)
        if is_sm6_like(self.dlna):
            return pick_sm6_control_device([self.dlna])
        return []

    def _sm6_poll_uuids_sync(self) -> list[str]:
        member_uuids = _virtual_member_uuids(self.dlna)
        if member_uuids:
            return list(member_uuids)
        uuid = getattr(self.dlna, "uuid", None)
        return [uuid] if uuid else []

    def _sm6_any_poll_paused_sync(self) -> bool:
        from dlna.sm6_soap_lane import poll_should_pause

        return any(poll_should_pause(uuid) for uuid in self._sm6_poll_uuids_sync())

    async def _sm6_send_key(self, key: str) -> None:
        from dlna.sm6_control import sm6_transport_key_pressed

        self._sm6_mark_outbound_activity()
        targets = await self._sm6_target_devices()
        devices = targets if targets else [self.dlna]
        for device in devices:
            await sm6_transport_key_pressed(device, key)

    @asynccontextmanager
    async def _sm6_transport_lane(self):
        from dlna.sm6_soap_lane import sm6_transport_command

        async with AsyncExitStack() as stack:
            for uuid in self._sm6_poll_uuids_sync():
                await stack.enter_async_context(sm6_transport_command(uuid))
            yield

    async def _sm6_volume_range(self):
        """Cached UPnP volume range from the physical SM6 target."""
        from dlna.sm6_volume import VolumeRange

        targets = await self._sm6_target_devices()
        device = targets[0] if targets else self.dlna
        await device.get_volume_info()
        return VolumeRange.from_device(device)

    async def _sm6_get_device_level(self) -> float | None:
        """Current SM6 device amplitude from GetVolume."""
        if not self._is_sm6_renderer():
            return None
        try:
            volume = await self.dlna.GetVolume()
        except Exception as exc:
            logger.debug("%s SM6 GetVolume failed: %s", self.dlna.name, exc)
            return None
        from dlna.sm6_volume import dlna_level_to_device

        volume_range = await self._sm6_volume_range()
        raw = getattr(volume, "CurrentVolume", None)
        return dlna_level_to_device(raw, volume_range)

    async def _sm6_read_device_step(self) -> int | None:
        """Current SM6 volume step from GetVolume (does not change Plex cache)."""
        level = await self._sm6_get_device_level()
        if level is None:
            return None
        from dlna.sm6_volume import device_to_step

        return device_to_step(level)

    async def _sm6_refresh_volume_from_device(self) -> int | None:
        """Read SM6 GetVolume and update state.volume; return volume step."""
        if not self._is_sm6_renderer():
            return None
        try:
            level = await self._sm6_get_device_level()
        except Exception as exc:
            logger.debug("%s SM6 volume refresh failed: %s", self.dlna.name, exc)
            return None
        if level is None:
            return None
        from dlna.sm6_volume import device_to_step, device_to_plex

        plex_vol = device_to_plex(level)
        self.state.volume = int(plex_vol)
        step = device_to_step(level)
        self.state.check_all_next_loop = True
        logger.debug(
            "%s SM6 volume refresh device=%.6f step=%s plex=%s%%",
            self.dlna.name,
            level,
            step,
            plex_vol,
        )
        return step

    async def _sm6_set_volume_on_device(self, device, desired: int) -> bool:
        from dlna.sm6_rendering_control import sm6_set_volume

        return await sm6_set_volume(device, desired)

    async def _sm6_should_skip_volume_poll(self) -> bool:
        from dlna.sm6_rendering_control import sm6_volume_poll_paused

        for device in await self._sm6_target_devices():
            if sm6_volume_poll_paused(device):
                return True
        return False

    def _sm6_schedule_volume_timeline_notify(self) -> None:
        """Notify Plex once volume has settled (Plexamp hardware buttons)."""
        task = self._sm6_volume_timeline_task
        if task is not None and not task.done():
            task.cancel()
        self._sm6_volume_timeline_task = asyncio.create_task(
            self._sm6_volume_timeline_notify_when_settled(),
            name=f"sm6-vol-notify-{self.dlna.name}",
        )

    async def _sm6_volume_timeline_notify_when_settled(self) -> None:
        try:
            await asyncio.sleep(settings.sm6_volume_debounce_seconds)
            while await self._sm6_should_skip_volume_poll():
                await asyncio.sleep(0.05)
            self.state.check_all_next_loop = True
            self._sm6_wake_waiters()
        except asyncio.CancelledError:
            return

    async def set_mute(self, muted: bool) -> None:
        targets = await self._sm6_target_devices()
        if targets:
            from dlna.sm6_rendering_control import sm6_set_mute

            ok = False
            for device in targets:
                ok = await sm6_set_mute(device, muted) or ok
            if not ok:
                logger.warning("%s SM6 SetMute failed", self.dlna.name)
                return
            self.state.muted = bool(muted)
            self.state.check_all_next_loop = True
            self._sm6_wake_waiters()
            return
        await self.dlna.SetMute(1 if muted else 0)
        self.state.muted = bool(muted)
        self.state.check_all_next_loop = True

    def _sm6_effective_track_uri(self, polled_uri: str | None) -> str | None:
        """Accept polled URI if it matches a known track (SM6 skip / auto-next)."""
        if not self._is_sm6_renderer():
            return polled_uri
        if not polled_uri:
            return self._sm6_session_uri
        from plex.url_resolver import get_url_resolver
        if get_url_resolver().rating_key_for_stream_url(polled_uri):
            return polled_uri
        return self._sm6_session_uri or polled_uri

    async def _sm6_rating_key_for_uri(self, uri: str) -> str | None:
        from plex.sm6_sync import rating_key_for_polled_uri

        return await rating_key_for_polled_uri(self, uri)

    async def _sm6_sync_from_polled_uri(self, uri: str) -> None:
        """Align the Plex queue with the track actually playing on the SM6."""
        if not self._is_sm6_renderer() or not uri:
            return
        if self._sm6_relinquished_control and not self._sm6_on_media_player_source():
            return
        if not await self._ensure_plex_lib_for_sm6_api():
            return
        rating_key = await self._sm6_rating_key_for_uri(uri)
        if not rating_key:
            return
        metadata = await self.plex_lib.fetch_metadata(f"/library/metadata/{rating_key}")
        if metadata is None:
            return
        if self.queue is not None:
            key = f"/library/metadata/{rating_key}"
            if not await self.queue.select_track_key(key):
                logger.debug(
                    "%s SM6 sync: ratingKey=%s hors fenêtre playQueue — metadata seule",
                    self.dlna.name,
                    rating_key,
                )
        track = metadata
        old_key = getattr(self.current_track_info, "ratingKey", None)
        if str(old_key) == str(getattr(track, "ratingKey", "")) and uri == self._sm6_session_uri:
            return
        self.current_track_info = track
        self._sm6_session_uri = uri
        logger.info(
            "%s SM6 sync Plex queue -> %s (ratingKey=%s)",
            self.dlna.name,
            getattr(track, "title", "?"),
            rating_key,
        )
        self._sm6_publish_track_change(track, uri=uri)

    def _sm6_passive_sync_blocked(self) -> bool:
        """True while Plex playMedia owns the adapter — passive SM6 sync must yield."""
        return self._sm6_plex_play_in_progress or self._sm6_sonoplay_owned_playback

    def _sm6_begin_plex_play(self) -> None:
        """Plex client requested playback — reclaim SM6 control from passive/external sync."""
        takeover = not self._sm6_sonoplay_owned_playback or self._sm6_relinquished_control
        self._sm6_relinquished_control = False
        self._sm6_sonoplay_owned_playback = True
        self._suppress_auto_next = False
        self._sm6_last_plex_playlist_fingerprint = None
        self._sm6_plex_play_in_progress = True
        self.state.update(state="TRANSITIONING")
        logger.info(
            "%s SM6 Plex play — reclaiming control (takeover=%s)",
            self.dlna.name,
            takeover,
        )

    def _sm6_abort_plex_play_takeover(self) -> None:
        """Restore passive SM6 sync after a failed or timed-out Plex playMedia."""
        self._sm6_sonoplay_owned_playback = False
        self._sm6_clear_optimistic_play()
        if self.state.state == "TRANSITIONING":
            self.state.update(state="STOPPED")
        self.state.check_all_next_loop = True
        logger.info(
            "%s SM6 Plex play failed — restoring passive sync",
            self.dlna.name,
        )
        if self.loop is not None and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._sm6_sync_external_playback(),
                self.loop,
            )

    def _sm6_mark_playback_owned(self) -> None:
        self._sm6_sonoplay_owned_playback = True
        self._sm6_relinquished_control = False
        self._sm6_last_plex_playlist_fingerprint = None

    def _sm6_publish_track_change(self, track, *, uri: str | None = None) -> None:
        """Push Plex title/duration as soon as the SM6 track changes."""
        if uri:
            self._sm6_session_uri = uri
        session_uri = self._sm6_session_uri
        if session_uri:
            self.state.update(uri=session_uri)
        self._sm6_wake_waiters()

    def _sm6_on_media_player_source(self) -> bool:
        from dlna.sm6_sources import AUDIO_SOURCE_MEDIA_PLAYER

        return self._sm6_last_audio_source == AUDIO_SOURCE_MEDIA_PLAYER

    def _sm6_mark_outbound_activity(self, seconds: float = 45.0) -> None:
        """Extend the window where poll ignores transient device STOP (SonoPlay commands)."""
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
            polled in _STOPPED_TRANSPORT_STATES
            and current in _ACTIVE_TRANSPORT_STATES
            and self._sm6_outbound_active()
        ):
            return current
        if polled == "PAUSED_PLAYBACK" and current == "PLAYING":
            until = getattr(self, "_sm6_optimistic_play_until", None)
            if until is not None and time.monotonic() < until:
                return current
            self._sm6_clear_optimistic_play()
        return polled

    def _sm6_observe_polled_transport(self, previous: str | None, polled: str) -> None:
        """Detect front-panel / remote transport changes and stop fighting the device."""
        if not self._is_sm6_renderer() or self._sm6_relinquished_control:
            return
        if self._sm6_plex_play_in_progress:
            return
        if self._sm6_outbound_active():
            return
        if polled in _ACTIVE_TRANSPORT_STATES and previous in _STOPPED_TRANSPORT_STATES:
            if self.loop is not None and not self.loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self._sm6_sync_external_playback(),
                    self.loop,
                )
            return
        if polled == "PAUSED_PLAYBACK" and previous in _ACTIVE_TRANSPORT_STATES:
            self._sm6_clear_optimistic_play()
            return
        if polled in _STOPPED_TRANSPORT_STATES and previous in _ACTIVE_TRANSPORT_STATES:
            self._sm6_relinquish_control("device_stopped")

    async def _sm6_sync_external_playback(self) -> None:
        """Playback started outside SonoPlay — sync metadata to Plex without taking control."""
        if not self._is_sm6_renderer():
            return
        if self._sm6_plex_play_in_progress:
            return
        self._sm6_sonoplay_owned_playback = False
        self._sm6_relinquished_control = False
        try:
            transport = await self.dlna.GetTransportInfo()
            transport_state = getattr(transport, "CurrentTransportState", None)
            if transport_state not in _ACTIVE_TRANSPORT_STATES:
                return
            position = await self.dlna.GetPositionInfo()
            uri = extract_value(getattr(position, "TrackURI", None), "")
            rel_time = extract_value(getattr(position, "RelTime", None), "0")
            self.state.update(state=transport_state, uri=uri or None, position=rel_time)
            if uri and not self._sm6_uri_is_plex_resolvable(uri):
                logger.debug(
                    "%s SM6 external sync skipped: URI not Plex/DLNA resolvable",
                    self.dlna.name,
                )
                return
            if uri:
                await self._sm6_sync_from_polled_uri(uri)
            from dlna.sm6_control import Sm6Control

            playlist_state = await Sm6Control(self.dlna.location_url).read_playlist_state(
                fetch_tracks=False,
            )
            self._sm6_playlist_snapshot = playlist_state
            if await self._sm6_is_playlist_queue_coherent(playlist_state):
                await self._sm6_maybe_sync_playlist(force=False)
            else:
                await self._sm6_rebuild_plex_playlist_from_sm6(force=False)
                await self._sm6_maybe_sync_playlist(force=False)
            self._sm6_wake_waiters()
        except Exception as exc:
            logger.debug("%s SM6 external playback sync failed: %s", self.dlna.name, exc)

    def _sm6_uri_is_plex_resolvable(self, uri: str | None) -> bool:
        from plex.sm6_sync import is_plex_resolvable_uri

        return is_plex_resolvable_uri(uri)

    async def _sm6_is_playlist_queue_coherent(self, playlist_state) -> bool:
        """True when Plex playQueue already mirrors the SM6 playlist snapshot."""
        if self.queue is None or playlist_state is None or playlist_state.length <= 0:
            return False
        fingerprint = self._sm6_playlist_fingerprint(playlist_state)
        if fingerprint != self._sm6_last_plex_playlist_fingerprint:
            return False
        if playlist_state.current_track_id != self._sm6_last_queue_track_id:
            return False
        try:
            plex_offset = await self.queue.selected_offset()
            expected = self._sm6_queue_base_offset + playlist_state.media_queue_index
            return plex_offset == expected
        except Exception:
            return False

    def _sm6_detach_plex_session(self) -> None:
        """Drop the Plex session view without stopping SM6 hardware playback."""
        self._suppress_auto_next = True

    def _sm6_clear_optimistic_play(self) -> None:
        self._sm6_optimistic_play_until = None
        task = getattr(self, "_sm6_play_notify_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._sm6_play_notify_task = None

    def _sm6_begin_optimistic_play(self) -> None:
        if not self._is_sm6_renderer():
            return
        self._sm6_optimistic_play_until = (
            time.monotonic() + settings.sm6_optimistic_play_seconds
        )
        self.state.arm_elapsed_assume(delay_seconds=0)
        task = self._sm6_play_notify_task
        if task is not None and not task.done():
            task.cancel()
        if self.loop is not None and not self.loop.is_closed():
            self._sm6_play_notify_task = asyncio.create_task(
                self._sm6_play_elapsed_pusher(),
                name=f"sm6-play-push-{self.dlna.name}",
            )

    async def _sm6_play_elapsed_pusher(self) -> None:
        """Wake Plex during the optimistic window to show extrapolation."""
        interval = settings.sm6_play_timeline_push_interval_seconds
        try:
            await asyncio.sleep(0.05)
            while (
                self._sm6_optimistic_play_until is not None
                and time.monotonic() < self._sm6_optimistic_play_until
            ):
                if self.state.state != "PLAYING" or not self.state._elapsed_assume_active:
                    break
                self._sm6_wake_waiters()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    def _sm6_wake_waiters(self) -> None:
        self.state.check_all_next_loop = True
        for entry in list(self.wait_state_change_events):
            entry["event"].set()
        if not self.no_notice and self.loop is not None and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._sm6_notify_plex_timeline(), self.loop)

    async def _sm6_notify_plex_timeline(self) -> None:
        from plex.subscribe import sub_man

        try:
            await sub_man.notify_device(self.dlna)
            await sub_man.notify_server_device(self.dlna, force=True)
        except Exception as exc:
            logger.debug("%s SM6 Plex timeline notify failed: %s", self.dlna.name, exc)

    async def _sm6_on_transcode_ready(self, rating_key: str) -> None:
        """Prefetch the next hi-res track only after the current one finished encoding."""
        if not self._is_sm6_renderer() or self.queue is None:
            return
        current_key = getattr(self.current_track_info, "ratingKey", None)
        if current_key is None or str(current_key) != str(rating_key):
            return
        next_track = await self._sm6_next_queue_track_after(str(rating_key))
        if next_track is None:
            return
        if not await self.queue.track_needs_transcode(next_track):
            return
        self._sm6_schedule_transcode_prefetch(next_track)

    async def _sm6_next_queue_track_after(self, rating_key: str):
        tracks = self._sm6_enqueued_tracks or tuple(await self._all_queue_tracks())
        for index, track in enumerate(tracks):
            if str(getattr(track, "ratingKey", "") or "") == str(rating_key):
                if index + 1 < len(tracks):
                    return tracks[index + 1]
                return None
        return None

    def _sm6_schedule_transcode_prefetch(self, track) -> None:
        task = self._sm6_transcode_prefetch_task
        if task is not None and not task.done():
            task.cancel()
        if self.loop is None or self.loop.is_closed():
            return
        self._sm6_transcode_prefetch_task = asyncio.create_task(
            self._sm6_prefetch_transcode_track(track),
        )

    async def _sm6_prefetch_transcode_track(self, track) -> None:
        from plex.mp3_transcode_cache import (
            cache_file_valid,
            cache_path_for,
            ensure_transcoded_mp3,
        )
        from plex.transcode_stream import resolve_pms_source_for_rating_key
        from settings import settings

        rating_key = str(getattr(track, "ratingKey", "") or "")
        if not rating_key.isdigit():
            return
        cbr_kbps = settings.audio_transcode_proxy_kbps
        cached = cache_path_for(rating_key, cbr_kbps=cbr_kbps)
        if cache_file_valid(cached):
            return
        source = await resolve_pms_source_for_rating_key(
            rating_key,
            device_uuid=self.dlna.uuid,
        )
        if source is None:
            return
        source_url, plex_token = source
        try:
            await ensure_transcoded_mp3(
                rating_key,
                source_url=source_url,
                cbr_kbps=cbr_kbps,
                plex_token=plex_token,
            )
            logger.info(
                "%s SM6 transcode prefetch ready ratingKey=%s title=%r",
                self.dlna.name,
                rating_key,
                getattr(track, "title", "?"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "%s SM6 transcode prefetch failed ratingKey=%s: %s",
                self.dlna.name,
                rating_key,
                exc,
            )

    def _track_info_for_plex(self, track) -> dict:
        info = {
            "duration": getattr(track, "duration", None),
            "key": getattr(track, "key", None),
            "ratingKey": getattr(track, "ratingKey", None),
            "title": getattr(track, "title", None),
            "artist": getattr(track, "grandparentTitle", None),
            "album": getattr(track, "parentTitle", None),
            "thumb": getattr(track, "thumb", None),
            "art": getattr(track, "art", None),
            "grandparentThumb": getattr(track, "grandparentThumb", None),
            "parentThumb": getattr(track, "parentThumb", None),
            "playQueueItemID": getattr(track, "playQueueItemID", None),
        }
        if self.queue is not None and self.queue.info is not None:
            info["containerKey"] = f"/playQueues/{self.queue.info.playQueueID}"
            info["playQueueID"] = self.queue.info.playQueueID
            info["playQueueVersion"] = self.queue.info.playQueueVersion
        return info

    def _sm6_entry_for_track_id(self, track_id: int, *, playlist_state=None):
        state = playlist_state or self._sm6_playlist_snapshot
        if state is not None:
            for entry in state.tracks:
                if entry.track_id == track_id:
                    return entry
        return None

    async def _sm6_refresh_playlist_snapshot(self, *, fetch_tracks: bool = True) -> None:
        if not self._is_sm6_renderer():
            return
        try:
            from dlna.sm6_control import Sm6Control

            state = await Sm6Control(self.dlna.location_url).read_playlist_state(
                fetch_tracks=fetch_tracks,
            )
            self._sm6_playlist_snapshot = state
            self._sm6_last_queue_track_id = state.current_track_id
            logger.debug(
                "%s SM6 playlist snapshot: length=%s current=%s index=%s",
                self.dlna.name,
                state.length,
                state.current_track_id,
                state.media_queue_index,
            )
        except Exception as exc:
            logger.debug("%s SM6 playlist snapshot refresh failed: %s", self.dlna.name, exc)

    async def _sm6_polled_track_uri(self) -> str | None:
        try:
            position = await self.dlna.GetPositionInfo()
            uri = extract_value(getattr(position, "TrackURI", None), "")
            return uri or self._sm6_session_uri
        except Exception:
            return self._sm6_session_uri

    async def _sm6_resolve_entry_to_track(
        self,
        entry,
        *,
        queue_index: int = -1,
        album_hint: str | None = None,
        fallback_uri: str | None = None,
    ):
        if self.queue is not None:
            if queue_index >= 0:
                plex_offset = self._sm6_queue_base_offset + queue_index
                total = await self.queue.total_count()
                if not math.isinf(total) and 0 <= plex_offset < total:
                    await self.queue.set_selected_offset(plex_offset)
                    return await self.queue.selected_track()
            if await self.queue.select_track_by_metadata(
                title=entry.title,
                artist=entry.artist,
                album=entry.album,
            ):
                return await self.queue.selected_track()
        track = await self.plex_lib.search_track(
            entry.title,
            artist=entry.artist,
            album=entry.album,
            album_hint=album_hint,
        )
        if track is not None:
            return track
        uri = fallback_uri or self._sm6_session_uri or ""
        rating_key = await self._sm6_rating_key_for_uri(uri)
        if rating_key:
            return await self.plex_lib.fetch_metadata(f"/library/metadata/{rating_key}")
        return None

    async def _sm6_apply_queue_track(
        self,
        track_id: int,
        *,
        entry=None,
        queue_index: int = -1,
    ) -> bool:
        if entry is None:
            entry = self._sm6_entry_for_track_id(track_id)
            if entry is None:
                try:
                    from dlna.sm6_control import Sm6Control

                    details = await Sm6Control(self.dlna.location_url).get_playlist_track_details(
                        start_track_id=track_id,
                        track_count=1,
                    )
                    entry = details[0] if details else None
                except Exception as exc:
                    logger.debug("%s SM6 fetch track %s failed: %s", self.dlna.name, track_id, exc)
                    return False
        if entry is None:
            return False

        track = await self._sm6_resolve_entry_to_track(entry, queue_index=queue_index)
        if track is None:
            logger.debug(
                "%s SM6 sync: impossible de résoudre %s / %s",
                self.dlna.name,
                entry.title,
                entry.artist,
            )
            return False

        old_key = getattr(self.current_track_info, "ratingKey", None)
        same_track = (
            str(old_key) == str(getattr(track, "ratingKey", ""))
            and track_id == self._sm6_last_queue_track_id
        )
        if same_track:
            if queue_index >= 0 and self.queue is not None:
                plex_offset = self._sm6_queue_base_offset + queue_index
                current = await self.queue.selected_offset()
                if current != plex_offset:
                    await self.queue.set_selected_offset(plex_offset)
                    self._sm6_publish_track_change(track)
            return False

        self.current_track_info = track
        self._sm6_last_queue_track_id = track_id
        from plex.url_resolver import get_url_resolver
        try:
            self._sm6_session_uri = await get_url_resolver().resolve_stream_url(track)
        except LookupError:
            pass
        logger.info(
            "%s SM6 sync playlist -> %s (ratingKey=%s, track_id=%s, index=%s)",
            self.dlna.name,
            getattr(track, "title", "?"),
            getattr(track, "ratingKey", "?"),
            track_id,
            queue_index,
        )
        self._sm6_publish_track_change(track)
        return True

    def _sm6_playlist_fingerprint(self, playlist_state) -> tuple | None:
        if playlist_state is None or playlist_state.length <= 0:
            return None
        entries = []
        for entry in playlist_state.tracks:
            entries.append(
                (
                    entry.track_id,
                    str(entry.title or "").casefold(),
                    str(entry.artist or "").casefold(),
                    str(entry.album or "").casefold(),
                )
            )
        return (
            playlist_state.length,
            playlist_state.current_track_id,
            playlist_state.media_queue_index,
            tuple(entries),
        )

    def _sm6_pick_selected_index(
        self,
        playlist_state,
        resolved_queue_map: list[int | None],
        *,
        rating_keys_len: int,
    ) -> int:
        """Map SM6 playback position to Plex playQueue selected index."""
        queue_index = playlist_state.media_queue_index
        if 0 <= queue_index < len(resolved_queue_map):
            mapped = resolved_queue_map[queue_index]
            if mapped is not None and 0 <= mapped < rating_keys_len:
                return mapped
        current_id = playlist_state.current_track_id
        for i, entry in enumerate(playlist_state.tracks):
            if entry.track_id == current_id and i < len(resolved_queue_map):
                mapped = resolved_queue_map[i]
                if mapped is not None and 0 <= mapped < rating_keys_len:
                    return mapped
        return 0

    async def _sm6_resolve_playlist_to_rating_keys(
        self,
        playlist_state,
    ) -> tuple[list[str], list, int]:
        """Map SM6 playlist entries to Plex rating keys; return keys, tracks, selected index."""
        polled_uri = await self._sm6_polled_track_uri()
        if polled_uri:
            self._sm6_session_uri = polled_uri

        rating_keys: list[str] = []
        tracks = []
        resolved_queue_map: list[int | None] = []
        last_album: str | None = None
        queue_index = playlist_state.media_queue_index
        current_id = playlist_state.current_track_id
        for i, entry in enumerate(playlist_state.tracks):
            entry_album = getattr(entry, "album", None)
            track = await self._sm6_resolve_entry_to_track(
                entry,
                queue_index=-1,
                album_hint=last_album if not entry_album else None,
                fallback_uri=(
                    polled_uri
                    if polled_uri and (i == queue_index or entry.track_id == current_id)
                    else None
                ),
            )
            if track is None:
                resolved_queue_map.append(None)
                logger.debug(
                    "%s SM6 playlist sync: skip unresolved %s / %s",
                    self.dlna.name,
                    entry.title,
                    entry.artist,
                )
                continue
            rk = str(getattr(track, "ratingKey", "") or "")
            if not rk:
                resolved_queue_map.append(None)
                continue
            resolved_queue_map.append(len(rating_keys))
            rating_keys.append(rk)
            tracks.append(track)
            if entry_album:
                last_album = str(entry_album)
            else:
                album = getattr(track, "parentTitle", None)
                if album:
                    last_album = str(album)

        selected_index = self._sm6_pick_selected_index(
            playlist_state,
            resolved_queue_map,
            rating_keys_len=len(rating_keys),
        )

        current_entry = None
        if 0 <= queue_index < len(playlist_state.tracks):
            current_entry = playlist_state.tracks[queue_index]
        if current_entry is None and current_id >= 0:
            current_entry = next(
                (entry for entry in playlist_state.tracks if entry.track_id == current_id),
                None,
            )
        if current_entry and tracks:
            selected_title = str(getattr(tracks[selected_index], "title", "")).casefold()
            if selected_title != str(current_entry.title).casefold():
                rk = await self._sm6_rating_key_for_uri(polled_uri or "")
                if rk:
                    if rk in rating_keys:
                        selected_index = rating_keys.index(rk)
                        logger.info(
                            "%s SM6 playlist sync: corrected selected via URI -> %s (index=%s)",
                            self.dlna.name,
                            getattr(tracks[selected_index], "title", "?"),
                            selected_index,
                        )
                    else:
                        metadata = await self.plex_lib.fetch_metadata(f"/library/metadata/{rk}")
                        if metadata is not None:
                            insert_at = min(selected_index, len(rating_keys))
                            rating_keys.insert(insert_at, rk)
                            tracks.insert(insert_at, metadata)
                            selected_index = insert_at
                            logger.info(
                                "%s SM6 playlist sync: inserted current track %s (ratingKey=%s, index=%s)",
                                self.dlna.name,
                                getattr(metadata, "title", "?"),
                                rk,
                                selected_index,
                            )

        if not rating_keys:
            return [], [], 0
        logger.info(
            "%s SM6 playlist selected index=%s (media_queue_index=%s, current_track_id=%s, title=%s)",
            self.dlna.name,
            selected_index,
            queue_index,
            current_id,
            getattr(tracks[selected_index], "title", "?") if tracks else "?",
        )
        return rating_keys, tracks, selected_index

    def _plex_lib_ready(self) -> bool:
        lib = self.plex_lib
        return bool(lib.protocol and lib.address and lib.port and lib.token)

    async def _bootstrap_pms_machine_id(self) -> None:
        if self.plex_lib.machine_id:
            return
        try:
            url = self.plex_lib.build_url("/identity")
            async with g.http.get(
                url,
                headers=self.plex_lib.request_headers(accept_json=True),
                timeout=settings.http_timeout_default,
            ) as res:
                res.raise_for_status()
                payload = await res.json()
            machine_id = (payload.get("MediaContainer") or {}).get("machineIdentifier")
            if machine_id:
                self.plex_lib.machine_id = machine_id
        except Exception as exc:
            logger.debug("%s PMS /identity failed: %s", self.dlna.name, exc)

    async def _sm6_plex_server_udn(self) -> str:
        from plex.dlna_browser import resolve_plex_dlna_server_udn
        from settings import settings

        machine_id = getattr(self.plex_lib, "machine_id", None) if self.plex_lib else None
        if not machine_id:
            await self._bootstrap_pms_machine_id()
            machine_id = getattr(self.plex_lib, "machine_id", None) if self.plex_lib else None
        return await resolve_plex_dlna_server_udn(
            settings.resolved_plex_dlna_device_url(),
            machine_identifier=machine_id,
        )

    def _resolve_sm6_pms_token(self) -> str | None:
        """PMS token for SM6 playlist rebuild (client session, device link, or env)."""
        if self.plex_lib.token:
            return self.plex_lib.token
        if self.plex_bind_token:
            return self.plex_bind_token
        from plex.transcode_stream import resolve_pms_token_for_proxy

        return resolve_pms_token_for_proxy(device_uuid=self.dlna.uuid)

    async def _ensure_plex_lib_for_sm6_api(self) -> bool:
        """Bootstrap Plex PMS URL/token when sync runs outside an active Plex client session."""
        if self._plex_lib_ready():
            return True
        token = self._resolve_sm6_pms_token()
        if not token:
            return False
        lib = self.plex_lib
        lib.token = token
        lib.device = self.dlna
        if not lib.client_identifier:
            lib.client_identifier = self.dlna.uuid

        if not lib.address or not lib.port:
            from plex.runtime_cache import cached_plex_session

            session = cached_plex_session()
            if session:
                if not lib.address:
                    lib.address = session["address"]
                if not lib.port:
                    lib.port = int(session["port"])
                if not lib.protocol:
                    lib.protocol = session["protocol"]
                if not lib.machine_id and session.get("machine_id"):
                    lib.machine_id = session["machine_id"]

        if not lib.address or not lib.port:
            device_url = settings.resolved_plex_dlna_device_url()
            if not device_url:
                return False
            from urllib.parse import urlparse

            parsed = urlparse(device_url)
            host = parsed.hostname
            if not host:
                return False
            if not lib.address:
                lib.address = host
            if not lib.port:
                lib.port = int(settings.plex_pms_port)
            if not lib.protocol:
                lib.protocol = parsed.scheme or "http"
        elif not lib.protocol:
            lib.protocol = "http"

        await self._bootstrap_pms_machine_id()
        logger.info(
            "%s SM6 bootstrap Plex PMS %s://%s:%s",
            self.dlna.name,
            lib.protocol,
            lib.address,
            lib.port,
        )
        return self._plex_lib_ready()

    async def _sm6_rebuild_plex_playlist_from_sm6(self, *, force: bool = False) -> bool:
        """Rebuild the Plex playQueue to mirror the SM6 native playlist."""
        if not self._is_sm6_renderer():
            return False
        if self._sm6_plex_play_in_progress and not force:
            return False
        if self._sm6_sonoplay_owned_playback and not force:
            return False
        if self._sm6_relinquished_control and not self._sm6_on_media_player_source():
            return False
        if time.monotonic() < self._sm6_playlist_rebuild_backoff_until:
            return False
        try:
            from dlna.sm6_control import Sm6Control
            from plex.play_queue import PlayQueue

            playlist_state = await Sm6Control(self.dlna.location_url).read_playlist_state(
                fetch_tracks=True,
            )
            if not force and self._sm6_passive_sync_blocked():
                logger.debug(
                    "%s SM6 rebuild aborted after read_playlist_state (Plex play in progress)",
                    self.dlna.name,
                )
                return False
            self._sm6_playlist_snapshot = playlist_state
            fingerprint = self._sm6_playlist_fingerprint(playlist_state)
            if not force and fingerprint == self._sm6_last_plex_playlist_fingerprint:
                return False
            if playlist_state.length <= 0:
                return False

            if not await self._ensure_plex_lib_for_sm6_api():
                self._sm6_playlist_rebuild_backoff_until = time.monotonic() + 30.0
                logger.debug(
                    "%s SM6 playlist sync: Plex PMS unavailable (token or host missing)",
                    self.dlna.name,
                )
                return False

            rating_keys, tracks, selected_index = await self._sm6_resolve_playlist_to_rating_keys(
                playlist_state,
            )
            if not force and self._sm6_passive_sync_blocked():
                logger.debug(
                    "%s SM6 rebuild aborted after resolve (Plex play in progress)",
                    self.dlna.name,
                )
                return False
            if not rating_keys:
                logger.debug("%s SM6 playlist sync: no resolvable tracks", self.dlna.name)
                return False

            queue = await PlayQueue.create_from_rating_keys(
                self.plex_lib,
                rating_keys,
                selected_index=selected_index,
                shuffle=self.shuffle,
                repeat=self._repeat_value(),
                sequential=True,
            )
            if not force and self._sm6_passive_sync_blocked():
                logger.debug(
                    "%s SM6 rebuild aborted before queue swap (Plex play in progress)",
                    self.dlna.name,
                )
                return False
            self.queue = queue
            self._sm6_queue_base_offset = 0
            self._sm6_last_queue_track_id = playlist_state.current_track_id
            self._sm6_last_plex_playlist_fingerprint = fingerprint
            self.current_track_info = await queue.selected_track()
            from plex.url_resolver import get_url_resolver
            try:
                self._sm6_session_uri = await get_url_resolver().resolve_stream_url(
                    self.current_track_info,
                )
            except LookupError:
                pass
            plex_total = await queue.total_count()
            logger.info(
                "%s SM6 → Plex playQueue rebuilt (%s Plex tracks from %d SM6 entries, "
                "selected=%s, title=%s)",
                self.dlna.name,
                plex_total if not math.isinf(plex_total) else len(rating_keys),
                playlist_state.length,
                selected_index,
                getattr(self.current_track_info, "title", "?"),
            )
            if (
                not math.isinf(plex_total)
                and int(plex_total) != len(rating_keys)
            ):
                logger.warning(
                    "%s SM6 playQueue count mismatch: Plex=%s resolved=%s SM6=%s",
                    self.dlna.name,
                    plex_total,
                    len(rating_keys),
                    playlist_state.length,
                )
            self._sm6_publish_track_change(self.current_track_info)
            self._sm6_playlist_rebuild_backoff_until = 0.0
            return True
        except Exception as exc:
            self._sm6_playlist_rebuild_backoff_until = time.monotonic() + 15.0
            logger.warning("%s SM6 rebuild Plex playQueue failed: %s", self.dlna.name, exc)
            return False

    async def _sm6_maybe_sync_playlist(self, *, force: bool = False) -> None:
        if not self._is_sm6_renderer():
            return
        if self._sm6_plex_play_in_progress and not force:
            return
        if self._sm6_relinquished_control and not self._sm6_on_media_player_source():
            return
        if not force and self._sm6_any_poll_paused_sync():
            return
        if not force and self.state.state not in _ACTIVE_TRANSPORT_STATES:
            return
        if not self._sm6_sonoplay_owned_playback:
            if not force and self._sm6_playlist_snapshot:
                if await self._sm6_is_playlist_queue_coherent(self._sm6_playlist_snapshot):
                    return
            if await self._sm6_rebuild_plex_playlist_from_sm6(force=force):
                return
        if not force and self._sm6_passive_sync_blocked():
            return
        try:
            from dlna.sm6_control import Sm6Control

            sm6 = Sm6Control(self.dlna.location_url)
            current_id, queue_index = await sm6.get_current_queue_position()
            if not force and self._sm6_passive_sync_blocked():
                return
            if not force and current_id == self._sm6_last_queue_track_id:
                if self.queue is not None:
                    plex_offset = await self.queue.selected_offset()
                    expected_index = plex_offset - self._sm6_queue_base_offset
                    if queue_index == expected_index:
                        return
                else:
                    return
            await self._sm6_apply_queue_track(current_id, queue_index=queue_index)
        except Exception as exc:
            logger.debug("%s SM6 playlist poll sync failed: %s", self.dlna.name, exc)

    async def _sm6_sync_after_skip(self) -> None:
        await asyncio.sleep(settings.sm6_position_assume_skip_delay_seconds)
        await self._sm6_maybe_sync_playlist(force=True)
        await self._sm6_refresh_playlist_snapshot()
        self._sm6_wake_waiters()
        await self._sm6_notify_plex_timeline()

    async def _sm6_bump_queue_offset(self, direction: int) -> None:
        if self.queue is None:
            return
        current = await self.queue.selected_offset()
        target = current + direction
        total = await self.queue.total_count()
        if not (0 <= target < total):
            return
        await self.queue.set_selected_offset(target)
        track = await self.queue.selected_track()
        self.current_track_info = track
        from plex.url_resolver import get_url_resolver
        try:
            self._sm6_session_uri = await get_url_resolver().resolve_stream_url(track)
        except LookupError:
            pass

    def _repeat_value(self) -> int:
        if self.queue is not None:
            return self.queue.repeat
        return self.repeat

    def _set_repeat_value(self, value: int) -> None:
        value = int(value)
        self.repeat = value
        if self.queue is not None:
            self.queue.repeat = value

    async def set_shuffle(self, value: int) -> None:
        """Apply Plex shuffle; on SM6, delegate to SetShuffle."""
        self.shuffle = int(value)
        if not self._is_sm6_renderer():
            return
        if self._sm6_relinquished_control:
            return
        from dlna.sm6_control import Sm6Control

        self._sm6_mark_outbound_activity()
        await Sm6Control(self.dlna.location_url).set_shuffle(self.shuffle > 0)
        self._sm6_wake_waiters()

    async def set_repeat(self, value: int) -> None:
        """Apply Plex repeat; on SM6, loop queue = SetRepeat (repeat=2)."""
        value = int(value)
        self._set_repeat_value(value)
        if not self._is_sm6_renderer():
            return
        if self._sm6_relinquished_control:
            return
        from dlna.sm6_control import Sm6Control

        # SM6: binary repeat (whole queue). repeat=1 (track) stays software SonoPlay.
        self._sm6_mark_outbound_activity()
        await Sm6Control(self.dlna.location_url).set_repeat(value == 2)
        self._sm6_wake_waiters()

    async def _sm6_poll_shuffle_repeat(self) -> None:
        if not self._is_sm6_renderer():
            return
        try:
            from dlna.sm6_control import Sm6Control

            sm6 = Sm6Control(self.dlna.location_url)
            shuffle_on, repeat_on = await asyncio.gather(sm6.get_shuffle(), sm6.get_repeat())
            new_shuffle = 1 if shuffle_on else 0
            new_repeat = 2 if repeat_on else (1 if self._repeat_value() == 1 else 0)
            changed = new_shuffle != self.shuffle or new_repeat != self._repeat_value()
            self.shuffle = new_shuffle
            if repeat_on or self._repeat_value() != 1:
                self._set_repeat_value(new_repeat)
            if changed:
                logger.info(
                    "%s SM6 shuffle/repeat poll -> shuffle=%s repeat=%s",
                    self.dlna.name,
                    self.shuffle,
                    self._repeat_value(),
                )
                self._sm6_wake_waiters()
        except Exception as exc:
            logger.debug("%s SM6 shuffle/repeat poll failed: %s", self.dlna.name, exc)

    def _sm6_relinquish_control(self, reason: str) -> None:
        """User took over on the SM6 (other source, front panel, etc.) — stop fighting."""
        already = self._sm6_relinquished_control
        if already and reason != "plex_stop":
            return
        if not already:
            logger.info("%s SM6 relinquishing Plex control (%s)", self.dlna.name, reason)
        self._sm6_relinquished_control = True
        if reason != "plex_stop":
            self._sm6_sonoplay_owned_playback = False
        self._sm6_outbound_until = None
        self._sm6_clear_optimistic_play()
        self._suppress_auto_next = True
        self._last_operation_finish_time = None
        self.state.update(state="STOPPED", uri=None)
        self._sm6_session_uri = None
        self.state.check_all_next_loop = True
        self._sm6_wake_waiters()

    async def _sm6_poll_audio_source(self) -> None:
        if not self._is_sm6_renderer() or self._sm6_relinquished_control:
            return
        from dlna.sm6_control import Sm6Control
        from dlna.sm6_sources import AUDIO_SOURCE_MEDIA_PLAYER

        try:
            current = await Sm6Control(self.dlna.location_url).get_current_audio_source_id()
        except Exception as exc:
            logger.debug("%s SM6 audio source poll failed: %s", self.dlna.name, exc)
            return
        if current is None:
            return
        previous = self._sm6_last_audio_source
        self._sm6_last_audio_source = current
        if current == AUDIO_SOURCE_MEDIA_PLAYER:
            return
        if previous == current:
            return
        active = self.state.state in _ACTIVE_TRANSPORT_STATES or self.queue is not None
        if active:
            self._sm6_relinquish_control(f"audio_source={current}")

    def _sm6_note_playback_start(self, seconds: float = 5.0) -> None:
        """Short window where Plex stale volume=0 must not reach the hardware."""
        deadline = time.monotonic() + seconds
        current = self._sm6_volume_grace_until or 0.0
        self._sm6_volume_grace_until = max(current, deadline)

    def _sm6_in_volume_grace_period(self) -> bool:
        return (
            self._sm6_volume_grace_until is not None
            and time.monotonic() < self._sm6_volume_grace_until
        )

    def _sm6_should_ignore_volume_write(self, requested: int) -> bool:
        """Reject Plex volume writes that disagree with the device during play startup."""
        if not self._is_sm6_renderer() or not self._sm6_in_volume_grace_period():
            return False
        device_plex = self.state.volume
        if device_plex is None:
            return False
        device_plex = int(device_plex)
        requested = int(requested)
        if requested == 0 and device_plex > 5:
            logger.info(
                "%s ignore volume=0 during play start (device=%s%%)",
                self.dlna.name,
                device_plex,
            )
            return True
        if abs(requested - device_plex) > 20:
            logger.info(
                "%s ignore stale volume=%s during play start (device=%s%%)",
                self.dlna.name,
                requested,
                device_plex,
            )
            return True
        return False

    async def _sm6_restore_volume_after_source_switch(self, preserved_level: float) -> None:
        """Re-apply level if switching to Media Player reset hardware volume."""
        if preserved_level <= 0:
            return
        await asyncio.sleep(0.15)
        after = await self._sm6_get_device_level()
        if after is None or after >= preserved_level - 0.05:
            return
        from dlna.sm6_volume import device_to_dlna_level, device_to_plex

        volume_range = await self._sm6_volume_range()
        desired = device_to_dlna_level(preserved_level, volume_range)
        targets = await self._sm6_target_devices()
        if not targets:
            return
        logger.info(
            "%s restoring volume after source switch (%.3f -> %.3f, DesiredVolume=%s)",
            self.dlna.name,
            preserved_level,
            after,
            desired,
        )
        self._sm6_mark_outbound_activity(5.0)
        for device in targets:
            await self._sm6_set_volume_on_device(device, desired)
        self.state.volume = int(device_to_plex(preserved_level))
        self.state.check_all_next_loop = True

    async def _ensure_sm6_ready(self, *, plex_takeover: bool = False) -> None:
        from dlna.sm6_control import Sm6Control
        from dlna.sm6_sources import AUDIO_SOURCE_MEDIA_PLAYER

        sm6 = Sm6Control(self.dlna.location_url)
        preserved_level = None
        if plex_takeover:
            logger.info("%s SM6 ensure_ready: Plex takeover path", self.dlna.name)
        else:
            logger.info("%s SM6 ensure_ready: reading audio source", self.dlna.name)
            previous_source = await sm6.get_current_audio_source_id()
            if previous_source is not None and previous_source != AUDIO_SOURCE_MEDIA_PLAYER:
                preserved_level = await self._sm6_get_device_level()

        self._sm6_relinquished_control = False
        self._sm6_sonoplay_owned_playback = True
        self._sm6_mark_outbound_activity()
        self._sm6_note_playback_start()
        logger.info("%s SM6 ensure_ready: power + Media Player source", self.dlna.name)
        await sm6.ensure_power_on()
        await sm6.ensure_media_player_source()

        if preserved_level is not None:
            await self._sm6_restore_volume_after_source_switch(preserved_level)
        if not plex_takeover:
            await self._sm6_refresh_volume_from_device()
        logger.info("%s SM6 ensure_ready: done", self.dlna.name)

    def state_changed_callback(self, changed_state: DotMap):
        if self.loop.is_closed():
            return
        if __debug__ or 'elapsed' not in changed_state.keys() or len(changed_state.keys()) > 2 or \
                not (0 <= changed_state.elapsed - changed_state.old.elapsed <= 1000):
            logger.debug("%s state change notified %s", self.dlna.name, changed_state.toDict())
        
        # Track PLAYING state for premature STOPPED detection
        if 'state' in changed_state and changed_state.state == "PLAYING":
            self._seen_playing_since_operation = True
        
        # Check for premature STOPPED that should be ignored (LMS-uPnP #63, go2tv #43)
        if self._should_ignore_premature_stopped(changed_state):
            self.state.update(state="TRANSITIONING")
            return
        
        if self._active_operation_id:
            if 'current_uri' in changed_state:
                if changed_state.current_uri == self._active_target_uri:
                    self._active_operation_uri_confirmed = True
                    if self._active_operation_state_ready and not self._active_operation_state_confirmed:
                        self._active_operation_state_confirmed = True
                        if self._active_operation_event:
                            self._active_operation_event.set()
                        logger.debug("%s transport operation %d uri confirmed", self.dlna.name, self._active_operation_id)
                        self._transport_state_override = None
                elif changed_state.current_uri is None and changed_state.old.get('current_uri') == self._active_target_uri:
                    logger.debug("%s ignoring transient URI clear during active transport operation", self.dlna.name)
                    self.state.update(uri=self._active_target_uri)
                    return
                elif changed_state.current_uri and changed_state.current_uri != self._active_target_uri:
                    if changed_state.old.get('current_uri') == self._active_target_uri:
                        if self._active_operation_uri_confirmed:
                            # Device previously confirmed our target URI, then reverted —
                            # genuine rejection.  Check if the original track was already
                            # un-playable (high-bitrate) to decide whether to skip.
                            if hasattr(self, 'current_track_info') and self.current_track_info:
                                if not self.queue.is_track_playable(self.current_track_info):
                                    logger.warning("%s device rejected track even after transcode attempt, skipping to next", self.dlna.name)
                                    # Cancel the active operation and skip to next track
                                    self._active_operation_id = None
                                    self._active_operation_event = None
                                    self._transport_state_override = None
                                    # Schedule next() to run in the event loop
                                    asyncio.run_coroutine_threadsafe(self.next(), self.loop)
                                    return
                        else:
                            # URI was never confirmed by the device — this "reversion"
                            # is a phantom from our internal state update racing with
                            # the polling cycle.  Restore the target and keep waiting.
                            logger.debug("%s ignoring phantom URI reversion (device never confirmed target)", self.dlna.name)
                        logger.debug("%s reverting URI %s -> restoring target %s", self.dlna.name, changed_state.current_uri, self._active_target_uri)
                        self.state.update(uri=self._active_target_uri)
                        return
                    if __debug__:
                        logger.debug("%s received uri %s while targeting %s", self.dlna.name, changed_state.current_uri, self._active_target_uri)
            if 'state' in changed_state:
                if changed_state.state in ("PLAYING", "PAUSED_PLAYBACK"):
                    self._active_operation_state_ready = True
                    if self._active_operation_uri_confirmed and not self._active_operation_state_confirmed:
                        self._active_operation_state_confirmed = True
                        if self._active_operation_event:
                            self._active_operation_event.set()
                        logger.debug("%s transport operation %d state confirmed", self.dlna.name, self._active_operation_id)
                        self._transport_state_override = None
                elif changed_state.state == "STOPPED" and not self._active_operation_state_confirmed:
                    logger.debug("%s ignoring STOP during active transport operation", self.dlna.name)
                    self.state.update(state="TRANSITIONING")
                    return
        # Post-operation protection: detect spurious STOPPED immediately after operation finish
        if self._check_post_operation_false_stop(changed_state):
            return
        n = self.check_auto_next(changed_state)
        if not n:
            asyncio.run_coroutine_threadsafe(self.state_changed(changed_state), self.loop)

    async def state_changed(self, changed_state: DotMap):
        removed_event = []
        for e in self.wait_state_change_events:
            fields = e['interesting_fields']
            matched = not fields
            if not matched:
                matched = any(f in changed_state.keys() for f in fields)
            if not matched and "elapsed_jump" in fields:
                if "elapsed" in changed_state:
                    delta = changed_state.elapsed - changed_state.old.elapsed
                    min_delta = int(settings.sm6_position_plex_notify_min_delta_ms)
                    matched = delta < 0 or delta >= min_delta
            if matched:
                e['event'].set()
                removed_event.append(e)
        for r in removed_event:
            self.wait_state_change_events.remove(r)
        self._update_stats(changed_state)

    async def wait_for_event(self, timeout=None, interesting_fields=None):
        self.state.touch_access_time()
        event = asyncio.Event()
        entry = dict(event=event, interesting_fields=interesting_fields)
        self.wait_state_change_events.append(entry)
        if len(self.wait_state_change_events) > 3:
            # Evict the OLDEST waiter, not the entry we just appended —
            # popping the newest would make every new long-poll return
            # immediately once 3 waiters exist.
            e = self.wait_state_change_events.pop(0)
            e['event'].set()
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.exceptions.TimeoutError:
            pass
        finally:
            # Always clean up the entry, whether event was set or timed out
            if entry in self.wait_state_change_events:
                self.wait_state_change_events.remove(entry)

    def _normalize_state(self, value):
        state_value = extract_value(value)
        if isinstance(state_value, str):
            return state_value.upper()
        return None

    def _update_stats(self, changed_state: DotMap):
        # Skip stat tracking if this device is being controlled by a virtual device
        if self._controlled_by_virtual_device:
            return
            
        if 'state' not in changed_state.keys():
            return
        now = datetime.now(timezone.utc)
        current_state = self._normalize_state(self.state.state)

        if current_state == "PLAYING" and self.stats_session_start is None:
            self.stats_session_start = now
            self.stats_play_count += 1
            _persist_stats(settings.increment_play_count, self.dlna.uuid)
        elif self.stats_session_start is not None and current_state != "PLAYING":
            elapsed = int((now - self.stats_session_start).total_seconds() * 1000)
            if elapsed > 0:
                self.stats_play_duration_ms += elapsed
                _persist_stats(settings.add_play_duration_ms, self.dlna.uuid, elapsed)
            self.stats_session_start = None
            _persist_stats(settings.mark_device_status, self.dlna.uuid, "online")
        else:
            status = "playing" if current_state == "PLAYING" else "online"
            _persist_stats(settings.mark_device_status, self.dlna.uuid, status)

    def stats_snapshot(self):
        playing = self.stats_session_start is not None and self._normalize_state(self.state.state) == "PLAYING"
        current_session_ms = 0
        if playing:
            current_session_ms = int((datetime.now(timezone.utc) - self.stats_session_start).total_seconds() * 1000)
        status = "playing" if playing else "online"
        return {
            "play_count": self.stats_play_count,
            "play_duration_ms": self.stats_play_duration_ms,
            "current_session_ms": current_session_ms,
            "status": status,
            "ip": self.dlna.ip
        }

    async def play_media(self, container_key, key=None, offset=0, paused=False, query_params: QueryParams = None):
        self.state.touch_access_time()
        if query_params is not None:
            self.plex_lib.update(query_params)

        sm6_play = self._is_sm6_renderer()
        if sm6_play:
            self._sm6_begin_plex_play()

        controller = self.virtual_controller()
        if controller is not None:
            try:
                controller.suspend_member(self, reason="solo playback request")
                controller_adapter = await adapter_by_device(controller)
                logger.info("%s releasing from virtual controller %s before solo playback", self.dlna.name, controller.name)
                await controller_adapter.stop(force=True)
            except Exception as exc:
                logger.warning("%s failed to stop virtual controller %s: %s", self.dlna.name, controller.name, exc)

        await self.dlna.get_data()

        self.state.update(uri=None)
        self.queue = self.plex_lib.get_queue(container_key)
        await self.queue.get_info()
        play_succeeded = False
        play_timeout = 90.0
        try:
            if key:
                logger.info("%s play_media requested key=%s", self.dlna.name, key)
                if not await self.queue.select_track_key(key):
                    metadata = await self.plex_lib.fetch_metadata(key)
                    if metadata and getattr(metadata, "type", None) == "album":
                        logger.info(
                            "%s play_media album key=%s title=%s",
                            self.dlna.name,
                            key,
                            getattr(metadata, "title", "?"),
                        )
                        await self._play_sm6_album(
                            metadata,
                            offset=offset,
                            paused=paused,
                            plex_takeover=sm6_play,
                        )
                        play_succeeded = True
                        return
                    logger.error(
                        "%s aborting play_media: key=%s not found in loaded queue window",
                        self.dlna.name,
                        key,
                    )
                    return
            if sm6_play:
                await asyncio.wait_for(
                    self.play_selected_queue_item(offset=offset, paused=paused),
                    timeout=play_timeout,
                )
            else:
                await self.play_selected_queue_item(offset=offset, paused=paused)
            play_succeeded = True
        except asyncio.TimeoutError:
            logger.error(
                "%s play_media timed out after %.0fs key=%s containerKey=%s",
                self.dlna.name,
                play_timeout if sm6_play else 0,
                key,
                container_key,
            )
        except Exception:
            logger.exception(
                "%s play_media failed key=%s containerKey=%s",
                self.dlna.name,
                key,
                container_key,
            )
            raise
        finally:
            if sm6_play:
                self._sm6_plex_play_in_progress = False
                if not play_succeeded:
                    self._sm6_abort_plex_play_takeover()

    async def play_selected_queue_item(self, offset=0, paused=False):
        # Get the track to check if it needs transcoding
        track = await self.queue.selected_track()
        part_key = None
        if hasattr(track, "Media") and track.Media and track.Media[0].Part:
            part_key = track.Media[0].Part[0].key
        logger.info(
            "%s playing track title=%s ratingKey=%s partKey=%s",
            self.dlna.name,
            getattr(track, "title", "?"),
            getattr(track, "ratingKey", "?"),
            part_key,
        )

        from plex.device_profiles import needs_plex_dlna_stream_url
        if needs_plex_dlna_stream_url(self.dlna):
            queue_tracks = await self._all_queue_tracks()
            if len(queue_tracks) <= 1:
                logger.info(
                    "%s SM6 single-track playMedia (queue len=%s) — PLAY_NOW only",
                    self.dlna.name,
                    len(queue_tracks),
                )
                await self._play_sm6_track(
                    track,
                    offset=offset,
                    paused=paused,
                    plex_takeover=self._sm6_plex_play_in_progress,
                )
                return
            mode = await self._detect_sm6_play_mode()
            total = await self.queue.total_count()
            logger.info(
                "%s SM6 play mode=%s queue_total=%s",
                self.dlna.name,
                mode,
                "?" if math.isinf(total) else total,
            )
            plex_takeover = self._sm6_plex_play_in_progress
            if plex_takeover:
                logger.info(
                    "%s SM6 Plex playMedia — mode=%s track=%r",
                    self.dlna.name,
                    mode,
                    getattr(track, "title", "?"),
                )
            if mode == "album":
                selected_offset = await self.queue.selected_offset()
                if selected_offset > 0:
                    logger.info(
                        "%s album play from track offset=%s — partial album as tracks, "
                        "full albums as containers",
                        self.dlna.name,
                        selected_offset,
                    )
                    await self._play_sm6_playlist(
                        offset=offset,
                        paused=paused,
                        plex_takeover=plex_takeover,
                    )
                else:
                    album_key = await self._sm6_album_rating_key()
                    metadata = await self.plex_lib.fetch_metadata(f"/library/metadata/{album_key}")
                    if metadata is None:
                        raise LookupError(f"Album metadata {album_key} not found")
                    await self._play_sm6_album(
                        metadata,
                        offset=offset,
                        paused=paused,
                        plex_takeover=plex_takeover,
                    )
            elif mode == "playlist":
                await self._play_sm6_playlist(
                    offset=offset,
                    paused=paused,
                    plex_takeover=plex_takeover,
                )
            else:
                await self._play_sm6_track(
                    track,
                    offset=offset,
                    paused=paused,
                    plex_takeover=plex_takeover,
                )
            return

        # Check if track needs transcoding due to high bitrate/sample rate
        needs_transcode = await self.queue.track_needs_transcode(track)
        if needs_transcode:
            title = getattr(track, 'title', 'Unknown')
            artist = getattr(track, 'grandparentTitle', 'Unknown Artist')
            media = track.Media[0] if hasattr(track, 'Media') and track.Media else None
            bitrate = getattr(media, 'bitrate', 'unknown') if media else 'unknown'
            sample_rate = getattr(media, 'audioSampleRate', 'unknown') if media else 'unknown'
            logger.info("%s high-bitrate track detected: '%s' by %s (%s kbps, %s Hz)",
                        self.dlna.name, title, artist, bitrate, sample_rate)
            logger.info("%s using Plex transcode for Sonos compatibility", self.dlna.name)
        
        async with self._transport_lock:
            self._transport_cancel_requested = False
            url = await self.queue.url_for_track(track, force_transcode=needs_transcode, dlna_device=self.dlna)
            operation_id = self._start_transport_operation(url)
            self._active_operation_target_paused = paused
            try:
                self.current_track_info = track
                attempt = 0
                while True:
                    attempt += 1
                    if attempt > 1:
                        logger.debug("%s retrying transport load attempt %d for %s", self.dlna.name, attempt, url)
                        self._reset_active_operation_tracking()
                    await self._issue_transport_commands(url, offset=offset if attempt == 1 else 0, paused=paused)
                    settled = await self._await_transport_settle(operation_id)
                    if self._transport_cancel_requested:
                        logger.info("%s transport operation %d cancelled by stop", self.dlna.name, operation_id)
                        break
                    if settled or attempt >= self._transport_max_attempts:
                        if not settled:
                            logger.warning("%s transport load timed out after %d attempts for %s", self.dlna.name, attempt, url)
                        break
            finally:
                self._finish_transport_operation(operation_id)
            self.current_track_info = track

    async def _detect_sm6_play_mode(self) -> str:
        from plex.sm6_queue_plan import detect_sm6_play_mode_from_tracks

        tracks = await self._all_queue_tracks()
        return detect_sm6_play_mode_from_tracks(tracks)

    async def _sm6_album_rating_key(self) -> str:
        track = await self.queue.selected_track()
        parent_key = getattr(track, "parentRatingKey", None)
        if not parent_key:
            raise LookupError("Album parentRatingKey missing on selected track")
        return str(parent_key)

    async def _all_queue_tracks(self):
        await self.queue.get_info()
        total = await self.queue.total_count()
        if math.isinf(total):
            return await self.queue.available_tracks()
        while True:
            last_offset = self.queue.last_offset
            if last_offset is None or last_offset + 1 >= total:
                break
            if not await self.queue.more(after=True):
                break
        return await self.queue.available_tracks()

    async def _sm6_album_track_counts(self, parent_keys: set[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for parent_key in parent_keys:
            if not parent_key:
                continue
            metadata = await self.plex_lib.fetch_metadata(f"/library/metadata/{parent_key}")
            if metadata is None:
                continue
            leaf_count = getattr(metadata, "leafCount", None)
            try:
                counts[parent_key] = int(leaf_count) if leaf_count is not None else 0
            except (TypeError, ValueError):
                counts[parent_key] = 0
        return counts

    async def _plan_sm6_queue_segments(self, tracks):
        from plex.sm6_queue_plan import plan_queue_segments

        parent_keys = {
            str(getattr(track, "parentRatingKey", "") or "")
            for track in tracks
            if getattr(track, "parentRatingKey", None)
        }
        album_counts = await self._sm6_album_track_counts(parent_keys)
        transcode_keys: set[str] = set()
        if self.queue is not None:
            for track in tracks:
                if await self.queue.track_needs_transcode(track):
                    rating_key = getattr(track, "ratingKey", None)
                    if rating_key is not None:
                        transcode_keys.add(str(rating_key))
        return plan_queue_segments(
            tracks,
            album_counts,
            is_track_playable=(
                (lambda track, keys=transcode_keys: str(getattr(track, "ratingKey", "")) not in keys)
                if transcode_keys
                else None
            ),
        )

    async def _sm6_resolve_track_playback(
        self,
        track,
        resolver,
        *,
        paused: bool = False,
        verbose: bool = True,
    ) -> tuple[str, str, int]:
        """DIDL, stream URL, and skip_count for one SM6 QueueFolder track item."""
        if self.queue is not None and await self.queue.track_needs_transcode(track):
            url = self.queue.build_sm6_transcode_proxy_url(track, device_uuid=self.dlna.uuid)
            from plex.url_resolver import build_transcode_track_didl

            didl = build_transcode_track_didl(track, url)
            if verbose:
                full = await self.plex_lib.fetch_metadata(
                    f"/library/metadata/{getattr(track, 'ratingKey', '')}",
                )
                media = full.Media[0] if full and getattr(full, "Media", None) else None
                sample_kbps, sample_hz = (
                    self.queue._media_playability_values(media) if media else (None, None)
                )
                logger.info(
                    "%s SM6 transcode QueueFolder track=%r ratingKey=%s (%s kbps, %s Hz) url=%s",
                    self.dlna.name,
                    getattr(track, "title", "?"),
                    getattr(track, "ratingKey", "?"),
                    sample_kbps if sample_kbps is not None else "?",
                    sample_hz if sample_hz is not None else "?",
                    self._redact_plex_token(url),
                )
            return didl, url, 0

        from plex.url_resolver import _DLNA_RESOLVE_TIMEOUT_SECONDS

        didl, skip_count = await asyncio.wait_for(
            resolver.resolve_play_didl(track, start_playback=not paused),
            timeout=_DLNA_RESOLVE_TIMEOUT_SECONDS,
        )
        url = await asyncio.wait_for(
            resolver.resolve_stream_url(track),
            timeout=_DLNA_RESOLVE_TIMEOUT_SECONDS,
        )
        return didl, url, skip_count

    @staticmethod
    def _redact_plex_token(url: str) -> str:
        import re

        return re.sub(r"(X-Plex-Token=)[^&]+", r"\1***", url)

    @staticmethod
    def _sm6_initial_queue_action(
        *,
        segment_kind: str,
        replace_transcode_queue: bool = False,
    ) -> str:
        from dlna.sm6_queue import initial_sm6_queue_action

        return initial_sm6_queue_action(
            segment_kind=segment_kind,
            replace_transcode_queue=replace_transcode_queue,
        )

    async def _play_sm6_track(
        self,
        track,
        *,
        offset: int = 0,
        paused: bool = False,
        plex_takeover: bool = False,
    ) -> None:
        from dlna.sm6_soap_lane import priority_transport_soap

        async def _dispatch() -> None:
            await self._play_sm6_track_impl(
                track,
                offset=offset,
                paused=paused,
                plex_takeover=plex_takeover,
            )

        await priority_transport_soap(str(self.dlna.uuid), _dispatch)

    async def _play_sm6_track_impl(
        self,
        track,
        *,
        offset: int = 0,
        paused: bool = False,
        plex_takeover: bool = False,
    ) -> None:
        from dlna.sm6_control import Sm6Control
        from dlna.sm6_queue import sm6_action_for_enqueue
        from plex.url_resolver import get_url_resolver
        from settings import settings

        logger.info(
            "%s SM6 ensure ready before track QueueFolder title=%r",
            self.dlna.name,
            getattr(track, "title", "?"),
        )
        await self._ensure_sm6_ready(plex_takeover=plex_takeover)
        resolver = get_url_resolver()
        rating_key = getattr(track, "ratingKey", "?")
        from plex.url_resolver import _DLNA_RESOLVE_TIMEOUT_SECONDS

        try:
            didl, url, skip_count = await self._sm6_resolve_track_playback(
                track,
                resolver,
                paused=paused,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Plex DLNA resolve timed out after {_DLNA_RESOLVE_TIMEOUT_SECONDS}s "
                f"for ratingKey={rating_key}"
            ) from exc

        server_udn = await self._sm6_plex_server_udn()
        sm6 = Sm6Control(self.dlna.location_url)
        action = sm6_action_for_enqueue("play")
        logger.info(
            "%s SM6 QueueFolder action=%s ratingKey=%s skip_count=%s",
            self.dlna.name,
            action,
            rating_key,
            skip_count,
        )
        await sm6.queue_folder(
            didl,
            action=action,
            server_udn=server_udn,
        )
        if skip_count and not paused:
            await self._sm6_skip_to_track(skip_count)

        self.current_track_info = track
        self._sm6_session_uri = url
        self._sm6_enqueued_tracks = (track,)
        selected = await self.queue.selected_offset() if self.queue else 0
        self._sm6_queue_base_offset = selected
        self.state.update(
            state="PAUSED_PLAYBACK" if paused else "PLAYING",
            uri=url,
            position=str(timedelta(milliseconds=offset)) if offset else "0",
        )
        await self._sm6_after_queue_folder(offset=offset, paused=paused)
        await self._sm6_refresh_playlist_snapshot()
        self._sm6_mark_playback_owned()
        self._sm6_notify_plex_timeline_sync()

    async def _play_sm6_album(
        self,
        album,
        *,
        offset: int = 0,
        paused: bool = False,
        plex_takeover: bool = False,
    ) -> None:
        from dlna.sm6_soap_lane import priority_transport_soap

        async def _dispatch() -> None:
            await self._play_sm6_album_impl(
                album,
                offset=offset,
                paused=paused,
                plex_takeover=plex_takeover,
            )

        await priority_transport_soap(str(self.dlna.uuid), _dispatch)

    async def _play_sm6_album_impl(
        self,
        album,
        *,
        offset: int = 0,
        paused: bool = False,
        plex_takeover: bool = False,
    ) -> None:
        from dlna.sm6_control import Sm6Control
        from dlna.sm6_queue import sm6_action_for_enqueue
        from plex.url_resolver import get_url_resolver
        from settings import settings

        logger.info(
            "%s SM6 ensure ready before album QueueFolder title=%r",
            self.dlna.name,
            getattr(album, "title", "?"),
        )
        await self._ensure_sm6_ready(plex_takeover=plex_takeover)
        resolver = get_url_resolver()
        album_key = str(getattr(album, "ratingKey", "") or "")
        album_tracks = [
            track
            for track in await self._all_queue_tracks()
            if str(getattr(track, "parentRatingKey", "") or "") == album_key
        ]
        if not album_tracks:
            album_tracks = await self.plex_lib.fetch_album_tracks(album_key)
        if album_tracks:
            needs_transcode_album = False
            for track in album_tracks:
                if await self.queue.track_needs_transcode(track):
                    needs_transcode_album = True
                    break
        else:
            needs_transcode_album = False
            logger.warning(
                "%s SM6 fetch_album_tracks empty for album %r (key=%s)",
                self.dlna.name,
                getattr(album, "title", "?"),
                album_key,
            )
        if needs_transcode_album:
            sample = album_tracks[0].Media[0] if getattr(album_tracks[0], "Media", None) else None
            sample_kbps, sample_hz = (
                self.queue._media_playability_values(sample) if sample else (None, None)
            )
            logger.info(
                "%s SM6 album %r has transcode tracks — track playlist instead of DLNA container "
                "(sample: %s kbps, %s Hz)",
                self.dlna.name,
                getattr(album, "title", "?"),
                sample_kbps if sample_kbps is not None else "?",
                sample_hz if sample_hz is not None else "?",
            )
            await self._play_sm6_playlist_impl(
                offset=offset,
                paused=paused,
                plex_takeover=plex_takeover,
            )
            return

        sample = None
        if album_tracks and getattr(album_tracks[0], "Media", None):
            sample = album_tracks[0].Media[0]
        sample_kbps, sample_hz = (
            self.queue._media_playability_values(sample) if sample else (None, None)
        )
        logger.info(
            "%s SM6 album %r direct DLNA container (sample: %s kbps, %s Hz, tracks=%s)",
            self.dlna.name,
            getattr(album, "title", "?"),
            sample_kbps if sample_kbps is not None else "?",
            sample_hz if sample_hz is not None else "?",
            len(album_tracks),
        )
        didl = await resolver.resolve_didl(album, media_kind="album")

        server_udn = await self._sm6_plex_server_udn()
        sm6 = Sm6Control(self.dlna.location_url)
        action = sm6_action_for_enqueue("replace")
        logger.info(
            "%s SM6 QueueFolder action=%s album=%r ratingKey=%s",
            self.dlna.name,
            action,
            getattr(album, "title", "?"),
            album_key,
        )
        await sm6.queue_folder(
            didl,
            action=action,
            server_udn=server_udn,
        )

        track = await self.queue.selected_track()
        url = await resolver.resolve_stream_url(track)
        self.current_track_info = track
        self._sm6_enqueued_tracks = tuple(album_tracks) if album_tracks else ()
        self._sm6_session_uri = url
        self._sm6_queue_base_offset = 0
        self.state.update(
            state="PAUSED_PLAYBACK" if paused else "PLAYING",
            uri=url,
            position=str(timedelta(milliseconds=offset)) if offset else "0",
        )
        await self._sm6_after_queue_folder(offset=offset, paused=paused)
        await self._sm6_refresh_playlist_snapshot()
        self._sm6_mark_playback_owned()
        self._sm6_notify_plex_timeline_sync()

    def _sm6_notify_plex_timeline_sync(self) -> None:
        if self.loop is not None and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._sm6_notify_plex_timeline(), self.loop)

    async def _play_sm6_playlist(
        self,
        *,
        offset: int = 0,
        paused: bool = False,
        plex_takeover: bool = False,
    ) -> None:
        from dlna.sm6_soap_lane import priority_transport_soap

        async def _dispatch() -> None:
            await self._play_sm6_playlist_impl(
                offset=offset,
                paused=paused,
                plex_takeover=plex_takeover,
            )

        await priority_transport_soap(str(self.dlna.uuid), _dispatch)

    async def _play_sm6_playlist_impl(
        self,
        *,
        offset: int = 0,
        paused: bool = False,
        plex_takeover: bool = False,
    ) -> None:
        from dlna.sm6_control import Sm6Control
        from dlna.sm6_queue import sm6_action_for_enqueue
        from plex.url_resolver import get_url_resolver
        from settings import settings

        await self._ensure_sm6_ready(plex_takeover=plex_takeover)
        resolver = get_url_resolver()
        server_udn = await self._sm6_plex_server_udn()

        sm6 = Sm6Control(self.dlna.location_url)
        self._sm6_mark_outbound_activity(90.0)

        selected_offset = await self.queue.selected_offset()
        tracks = await self._all_queue_tracks()
        local_start = self.queue.start_offset or 0
        start_index = max(0, selected_offset - local_start)
        queue_tracks = tracks[start_index:]

        if not queue_tracks:
            raise LookupError("SM6 playlist is empty")

        segments = await self._plan_sm6_queue_segments(queue_tracks)
        track_segments = sum(1 for segment in segments if segment.kind == "track")
        album_segments = sum(1 for segment in segments if segment.kind == "album")
        logger.info(
            "%s SM6 queue plan: %s track(s) -> %s segment(s) "
            "(%s album container(s), %s track SOAP call(s))",
            self.dlna.name,
            len(queue_tracks),
            len(segments),
            album_segments,
            track_segments,
        )

        first_track = queue_tracks[0]
        _, first_url, _ = await self._sm6_resolve_track_playback(
            first_track,
            resolver,
            paused=paused,
        )

        transcode_track_playlist = track_segments > 0 and album_segments == 0
        if transcode_track_playlist and self.queue is not None:
            for segment in segments:
                if segment.kind != "track":
                    continue
                if not await self.queue.track_needs_transcode(segment.tracks[0]):
                    transcode_track_playlist = False
                    break

        for segment_index, segment in enumerate(segments):
            action = (
                self._sm6_initial_queue_action(
                    segment_kind=segment.kind,
                    replace_transcode_queue=transcode_track_playlist,
                )
                if segment_index == 0
                else sm6_action_for_enqueue("add")
            )
            if segment.kind == "album":
                if not segment.parent_rating_key:
                    raise LookupError("Album segment without parentRatingKey")
                album_metadata = await self.plex_lib.fetch_metadata(
                    f"/library/metadata/{segment.parent_rating_key}",
                )
                if album_metadata is None:
                    raise LookupError(
                        f"Album metadata {segment.parent_rating_key} not found",
                    )
                didl = await resolver.resolve_didl(album_metadata, media_kind="album")
                logger.info(
                    "%s SM6 QueueFolder %s album parent=%s (%s track(s))",
                    self.dlna.name,
                    action,
                    segment.parent_rating_key,
                    len(segment.tracks),
                )
            else:
                track = segment.tracks[0]
                didl, _, _ = await self._sm6_resolve_track_playback(
                    track,
                    resolver,
                    paused=paused,
                )
            await sm6.queue_folder(
                didl,
                action=action,
                server_udn=server_udn,
            )
            self._sm6_mark_outbound_activity(15.0)

        self._sm6_enqueued_tracks = tuple(queue_tracks)
        self._sm6_synced_tail_item_ids = tuple(
            int(track.playQueueItemID)
            for track in queue_tracks[1:]
            if getattr(track, "playQueueItemID", None) is not None
        )
        logger.info(
            "%s SM6 queue enqueued %d track(s) via %d QueueFolder call(s)",
            self.dlna.name,
            len(queue_tracks),
            len(segments),
        )

        self.current_track_info = first_track
        self._sm6_session_uri = first_url
        self._sm6_queue_base_offset = start_index
        self.state.update(
            state="PAUSED_PLAYBACK" if paused else "PLAYING",
            uri=first_url,
            position=str(timedelta(milliseconds=offset)) if offset else "0",
        )
        await self._sm6_after_queue_folder(offset=offset, paused=paused)
        await self._sm6_refresh_playlist_snapshot()
        self._sm6_mark_playback_owned()
        self._sm6_notify_plex_timeline_sync()

    async def _sm6_skip_to_track(self, count: int) -> None:
        from dlna.sm6_control import Sm6Control
        from plex.url_resolver import _SM6_ALBUM_PLAY_SKIP_SETTLE_SECONDS

        sm6 = Sm6Control(self.dlna.location_url)
        for index in range(count):
            logger.info(
                "%s SM6 skip_next %d/%d after album QueueFolder",
                self.dlna.name,
                index + 1,
                count,
            )
            await sm6.skip_next()
            await asyncio.sleep(_SM6_ALBUM_PLAY_SKIP_SETTLE_SECONDS)

    async def _sm6_after_queue_folder(self, *, offset: int = 0, paused: bool = False) -> None:
        """Update Plex timeline after QueueFolder PLAY_NOW (SM6 starts transport itself)."""
        self.state.check_all_next_loop = True
        # ponytail: SM6 AVTransport Seek returns UPnPError after QueueFolder PLAY_NOW
        if offset:
            logger.debug(
                "%s SM6 skipping Seek(%s) after QueueFolder",
                self.dlna.name,
                offset,
            )
        if paused:
            await self._sm6_pause()
            return
        self._sm6_begin_optimistic_play()
        self._sm6_mark_outbound_activity()

    async def _sm6_pause(self) -> None:
        logger.info("%s SM6 pause via KeyPressed PLAY_PAUSE", self.dlna.name)
        self._sm6_clear_optimistic_play()
        self.state.disarm_elapsed_assume()
        self.state.update(state="PAUSED_PLAYBACK")
        self.state.check_all_next_loop = True
        self._sm6_wake_waiters()
        await self._sm6_send_key("PLAY_PAUSE")

    async def _sm6_play(self) -> None:
        if self._sm6_relinquished_control:
            self._sm6_relinquished_control = False
            self._suppress_auto_next = False
        self._sm6_mark_playback_owned()
        self._sm6_note_playback_start()
        logger.info("%s SM6 resume via KeyPressed PLAY_PAUSE", self.dlna.name)
        self.state.update(state="PLAYING")
        self._sm6_begin_optimistic_play()
        self.state.check_all_next_loop = True
        self._sm6_wake_waiters()
        await self._sm6_send_key("PLAY_PAUSE")

    async def _play_sm6_queue_folder(self, track, *, offset: int = 0, paused: bool = False) -> None:
        await self._play_sm6_track(track, offset=offset, paused=paused)

    def _reset_active_operation_tracking(self) -> None:
        if not self._active_operation_id or not self._active_target_uri:
            return
        self._active_operation_event = asyncio.Event()
        self._active_operation_uri_confirmed = False
        self._active_operation_state_ready = False
        self._active_operation_state_confirmed = False
        self._transport_state_override = {
            "state": "TRANSITIONING",
            "current_uri": self._active_target_uri
        }

    async def _issue_transport_commands(self, url: str, *, offset: int, paused: bool) -> None:
        self.state.update(state="TRANSITIONING")
        self.state.check_all_next_loop = True
        if url == self.state.current_uri:
            self.state.update(uri=None)
        else:
            self.state.update(uri=url)
        logger.info("%s SetAVTransportURI: %s", self.dlna.name, url)
        await self.dlna.SetAVTransportURI(url)
        if offset != 0:
            self.state.update(position=str(timedelta(milliseconds=offset)))
            await self.dlna.Seek(str(timedelta(milliseconds=offset)))
        else:
            self.state.update(position="0")
        if paused:
            await self.pause()
        else:
            # Poll GetCurrentTransportActions until the device reports Play
            # is available (devices that lack the action return immediately).
            wait_ready = getattr(self.dlna, "wait_for_can_play", None)
            if wait_ready is not None:
                await wait_ready(max_wait=2.0)
            else:
                await asyncio.sleep(0.1)
            await self.play()

    async def _await_transport_settle(self, operation_id: int) -> bool:
        if self._active_operation_id != operation_id or self._active_operation_event is None:
            return True
        try:
            await asyncio.wait_for(self._active_operation_event.wait(), timeout=self._transport_settle_timeout)
            logger.debug("%s transport operation %d settled", self.dlna.name, operation_id)
        except asyncio.TimeoutError:
            logger.debug("%s transport operation %d timed out waiting for settle", self.dlna.name, operation_id)
            return False
        return self._active_operation_state_confirmed and self._active_operation_id == operation_id

    async def _sm6_plex_tail_snapshot(self) -> tuple[list[int], list]:
        """playQueueItemIDs and tracks after the current Plex selection."""
        tracks = await self._all_queue_tracks()
        selected = await self.queue.selected_offset()
        local_start = self.queue.start_offset or 0
        start_index = max(0, selected - local_start)
        tail_tracks = tracks[start_index + 1 :]
        tail_ids = [
            int(track.playQueueItemID)
            for track in tail_tracks
            if getattr(track, "playQueueItemID", None) is not None
        ]
        return tail_ids, tail_tracks

    async def _sm6_resolve_track_didl(self, track) -> str:
        from plex.url_resolver import build_transcode_track_didl, get_url_resolver

        if self.queue is not None and await self.queue.track_needs_transcode(track):
            url = self.queue.build_sm6_transcode_proxy_url(track, device_uuid=self.dlna.uuid)
            return build_transcode_track_didl(track, url)
        resolver = get_url_resolver()
        return await resolver.resolve_didl(track, media_kind="track")

    async def _sm6_apply_queue_edit_op(
        self,
        sm6,
        op,
        *,
        plex_tail_tracks: list,
    ) -> None:
        from plex.sm6_queue_edit import (
            Sm6ClearQueueTail,
            Sm6DeleteTrack,
            Sm6InsertTrack,
            Sm6MoveTrack,
        )

        if isinstance(op, Sm6ClearQueueTail):
            logger.info(
                "%s refreshPlayQueue SM6 DeleteAll (clear upcoming queue)",
                self.dlna.name,
            )
            await sm6.clear_queue()
            return
        if isinstance(op, Sm6DeleteTrack):
            logger.info(
                "%s refreshPlayQueue SM6 DeletePlaylistTrack id=%s",
                self.dlna.name,
                op.sm6_track_id,
            )
            await sm6.delete_playlist_track(playlist_track_id=op.sm6_track_id)
            return
        if isinstance(op, Sm6MoveTrack):
            logger.info(
                "%s refreshPlayQueue SM6 MovePlaylistTrack from=%s to=%s",
                self.dlna.name,
                op.from_index,
                op.to_index,
            )
            await sm6.move_playlist_track(from_index=op.from_index, to_index=op.to_index)
            return
        if isinstance(op, Sm6InsertTrack):
            track = plex_tail_tracks[op.plex_tail_index]
            didl = await self._sm6_resolve_track_didl(track)
            logger.info(
                "%s refreshPlayQueue SM6 InsertPlaylistTrack pos=%s title=%s ratingKey=%s",
                self.dlna.name,
                op.insert_position,
                getattr(track, "title", "?"),
                getattr(track, "ratingKey", "?"),
            )
            await sm6.insert_playlist_track(
                insert_position=op.insert_position,
                didl=didl,
            )
            self._sm6_enqueued_tracks = (*self._sm6_enqueued_tracks, track)

    async def _sm6_sync_queue_on_refresh(
        self,
        old_tail_item_ids: list[int],
        selected_item_id_before: int | None,
    ) -> None:
        """Mirror Plex playQueue edits (insert/delete/move/clear tail) on the SM6."""
        if not self._is_sm6_renderer() or self.queue is None:
            return
        if self._sm6_relinquished_control:
            return
        if self.state.state not in ("PLAYING", "PAUSED_PLAYBACK"):
            return

        selected_item_id_after = await self.queue.selected_item_id()
        if (
            selected_item_id_before is not None
            and selected_item_id_after != selected_item_id_before
        ):
            logger.debug(
                "%s refreshPlayQueue: current track changed (%s -> %s), skipping SM6 sync",
                self.dlna.name,
                selected_item_id_before,
                selected_item_id_after,
            )
            return

        new_tail_ids, new_tail_tracks = await self._sm6_plex_tail_snapshot()
        if old_tail_item_ids == new_tail_ids:
            return

        from dlna.sm6_control import Sm6Control
        from plex.sm6_queue_edit import (
            Sm6ClearQueueTail,
            Sm6InsertTrack,
            plan_deletes_for_removed_items,
            reconcile_sm6_tail_to_plex,
            sm6_tail_entries,
        )

        sm6 = Sm6Control(self.dlna.location_url)
        await self._sm6_refresh_playlist_snapshot(fetch_tracks=True)
        state = self._sm6_playlist_snapshot
        sm6_tail = sm6_tail_entries(state)
        sm6_tail_ids = [entry.track_id for entry in sm6_tail]

        delete_ops = plan_deletes_for_removed_items(
            old_tail_item_ids=old_tail_item_ids,
            new_tail_item_ids=new_tail_ids,
            sm6_tail_track_ids=sm6_tail_ids,
        )
        cleared_tail = any(isinstance(op, Sm6ClearQueueTail) for op in delete_ops)
        for op in delete_ops:
            await self._sm6_apply_queue_edit_op(sm6, op, plex_tail_tracks=new_tail_tracks)

        if delete_ops:
            await self._sm6_refresh_playlist_snapshot(fetch_tracks=True)
            state = self._sm6_playlist_snapshot

        old_set = set(old_tail_item_ids)
        new_set = set(new_tail_ids)
        survivors_match = (
            [item_id for item_id in old_tail_item_ids if item_id in new_set]
            == [item_id for item_id in new_tail_ids if item_id in old_set]
        )
        added_only = (
            not cleared_tail
            and survivors_match
            and len(new_tail_ids) > len(old_tail_item_ids)
        )

        if added_only:
            queue_index = state.media_queue_index if state else 0
            new_tracks = [
                track
                for track in new_tail_tracks
                if int(track.playQueueItemID) not in old_set
            ]
            insert_ops = sorted(
                (
                    Sm6InsertTrack(
                        insert_position=queue_index + 1 + new_tail_ids.index(
                            int(track.playQueueItemID),
                        ),
                        plex_tail_index=new_tail_ids.index(int(track.playQueueItemID)),
                    )
                    for track in new_tracks
                ),
                key=lambda op: op.insert_position,
            )
            for op in insert_ops:
                await self._sm6_apply_queue_edit_op(sm6, op, plex_tail_tracks=new_tail_tracks)
                await self._sm6_refresh_playlist_snapshot(fetch_tracks=True)
                state = self._sm6_playlist_snapshot
        else:
            from plex.sm6_queue_edit import Sm6ClearQueueTail, Sm6InsertTrack, Sm6MoveTrack

            if cleared_tail and not new_tail_tracks:
                reconcile_ops = []
            else:
                reconcile_ops = reconcile_sm6_tail_to_plex(
                    plex_tail_tracks=new_tail_tracks,
                    sm6_state=state,
                )
            for op in reconcile_ops:
                await self._sm6_apply_queue_edit_op(sm6, op, plex_tail_tracks=new_tail_tracks)
                if isinstance(op, (Sm6InsertTrack, Sm6MoveTrack, Sm6ClearQueueTail)):
                    await self._sm6_refresh_playlist_snapshot(fetch_tracks=True)
                    state = self._sm6_playlist_snapshot
                if isinstance(op, Sm6ClearQueueTail):
                    break

        self._sm6_synced_tail_item_ids = tuple(new_tail_ids)
        await self._sm6_refresh_playlist_snapshot(fetch_tracks=False)
        self._sm6_wake_waiters()

    async def refresh_queue(self, playQueueID):
        old_tail_item_ids: list[int] = []
        selected_item_id_before = None
        if self.queue is not None:
            try:
                if self._is_sm6_renderer():
                    old_tail_item_ids, _ = await self._sm6_plex_tail_snapshot()
                selected_item_id_before = await self.queue.selected_item_id()
            except Exception as exc:
                logger.debug("%s refreshPlayQueue snapshot failed: %s", self.dlna.name, exc)

        await self.queue.refresh_queue(playQueueID)

        if self._is_sm6_renderer() and self.queue is not None:
            info = await self.queue.get_info()
            plex_shuffle = int(getattr(info, "playQueueShuffled", 0) or 0)
            if plex_shuffle != self.shuffle:
                logger.info(
                    "%s refreshPlayQueue shuffle Plex=%s -> SM6",
                    self.dlna.name,
                    plex_shuffle,
                )
                await self.set_shuffle(plex_shuffle)
            await self._sm6_sync_queue_on_refresh(
                old_tail_item_ids,
                selected_item_id_before,
            )
        while len(self.wait_state_change_events) > 0:
            e = self.wait_state_change_events.pop()
            e['event'].set()

    async def play(self):
        if self._is_sm6_renderer():
            await self._sm6_play()
            return
        await self.dlna.Play()
        self.state.check_all_next_loop = True

    async def stop(self, *, force: bool = False):
        # Guard against stale Plex stop commands during auto-next transition (non-SM6 only).
        if not force and self._auto_next_in_flight and not self._is_sm6_renderer():
            logger.info("%s ignoring stale stop command during auto-next transition", self.dlna.name)
            return
        controller = self.virtual_controller()
        if controller is not None and not force:
            logger.info("%s stop request rerouted to virtual device %s", self.dlna.name, controller.name)
            await controller.handle_member_stop_request(self)
            return
        # Abort any in-flight transport operation so we don't wait up to
        # ~18s (retries x settle timeout) for the lock while the user's
        # stop appears to hang.
        self._transport_cancel_requested = True
        settle_event = self._active_operation_event
        if settle_event is not None:
            settle_event.set()
        async with self._transport_lock:
            active_id = self._active_operation_id
            if active_id:
                self._finish_transport_operation(active_id)
            self._suppress_auto_next = True
            # Intentional stop: clear post-operation timestamp so false-STOP
            # detection does not misinterpret the resulting STOPPED event
            self._last_operation_finish_time = None
        self.state.update(state="STOPPED", uri=None)
        self.current_track_info = None
        self._sm6_session_uri = None
        self._sm6_clear_optimistic_play()
        if force:
            self.queue = None
        if self._is_sm6_renderer():
            self._sm6_relinquish_control("plex_stop")
            self._sm6_sonoplay_owned_playback = False
            logger.info("%s SM6 stop — KeyPressed STOP (plex)", self.dlna.name)
            async with self._sm6_transport_lane():
                from dlna.sm6_control import Sm6Control

                await Sm6Control(self.dlna.location_url).stop()
            self.state.check_all_next_loop = True
            return
        await self.dlna.Stop()
        self.state.check_all_next_loop = True

    async def pause(self):
        if self._is_sm6_renderer():
            await self._sm6_pause()
            return
        # Only send Pause command if currently playing - avoids UPnP error 701
        # (Transition not available) when device is already stopped/paused
        if self.state.state == "PLAYING":
            await self.dlna.Pause()
        self.state.update(state="PAUSED_PLAYBACK")
        self.state.check_all_next_loop = True

    async def prev(self):
        if self.state.elapsed <= 5 * 1000:
            await self.next(revert=True)
        elif self._is_sm6_renderer():
            async with self._sm6_transport_lane():
                await self.seek(0)
        else:
            await self.seek(0)

    async def next(self, revert=False):
        if self._is_sm6_renderer() and self.queue is not None:
            mode = await self._detect_sm6_play_mode()
            if mode in ("album", "playlist"):
                from dlna.sm6_simple_remote import KEY_SKIP_NEXT, KEY_SKIP_PREVIOUS

                if revert:
                    await self._sm6_send_key(KEY_SKIP_PREVIOUS)
                else:
                    await self._sm6_send_key(KEY_SKIP_NEXT)
                self.state.disarm_elapsed_assume()
                self.state.update(state="TRANSITIONING", position="0")
                self.state.arm_elapsed_assume(
                    elapsed_ms=0,
                    delay_seconds=settings.sm6_position_assume_skip_delay_seconds,
                )
                self.state.check_all_next_loop = True
                asyncio.create_task(self._sm6_sync_after_skip())
                return
        direction = -1 if revert else 1
        current_offset = await self.queue.selected_offset()
        total_count = await self.queue.total_count()

        if self.shuffle > 0 and await self.queue.allow_shuffle():
            if math.isinf(total_count):
                await self.queue.get_info()
                available_count = await self.queue.available_count()
                if available_count <= 0:
                    await self.stop()
                    return
                start_offset = self.queue.start_offset or 0
                current_offset = start_offset + random.randrange(available_count)
            else:
                current_offset = random.randrange(int(total_count))
        else:
            current_offset += direction

        start_offset = self.queue.start_offset
        last_offset = self.queue.last_offset if self.queue.last_offset is not None else "unknown"
        logger.debug("%s next diagnostics current=%s last=%s start=%s total=%s direction=%s",
                     self.dlna.name, current_offset, last_offset, start_offset, total_count, direction)

        if current_offset < 0:
            logger.debug("%s next guard stop: offset<0 current=%s start=%s", self.dlna.name, current_offset, start_offset)
            self._auto_next_in_flight = False
            await self.stop()
            return
        if not math.isinf(total_count) and current_offset >= total_count:
            logger.debug("%s next guard stop: offset>=total current=%s total=%s", self.dlna.name, current_offset, total_count)
            self._auto_next_in_flight = False
            await self.stop()
            return
        self.state.update(state="TRANSITIONING")
        logger.debug("will play position %s of %s", current_offset, total_count)
        await self.queue.set_selected_offset(current_offset)
        logger.debug("%s next invoking play_selected_queue_item offset=%s", self.dlna.name, current_offset)
        await self.play_selected_queue_item()

    async def _sm6_try_select_playlist_track(self) -> bool:
        """Select a track already present in the SM6 queue (native API)."""
        if not self._is_sm6_renderer() or self.queue is None:
            return False
        plex_offset = await self.queue.selected_offset()
        queue_index = plex_offset - self._sm6_queue_base_offset
        length = getattr(self._sm6_playlist_snapshot, "length", 0) or 0
        from dlna.sm6_control import Sm6Control
        from dlna.sm6_playlist import sm6_set_current_playlist_track_id
        from plex.url_resolver import get_url_resolver

        sm6 = Sm6Control(self.dlna.location_url)
        if length <= 0:
            length = await sm6.get_playlist_length()
        if queue_index < 0 or queue_index >= length:
            return False

        await self._sm6_refresh_playlist_snapshot()
        entry = None
        state = self._sm6_playlist_snapshot
        if state is not None and queue_index < len(state.tracks):
            entry = state.tracks[queue_index]
        sm6_track_id = sm6_set_current_playlist_track_id(queue_index, entry)

        await sm6.set_current_playlist_track(sm6_track_id)
        track = await self.queue.selected_track()
        self.current_track_info = track
        self._sm6_last_queue_track_id = sm6_track_id
        try:
            self._sm6_session_uri = await get_url_resolver().resolve_stream_url(track)
        except LookupError:
            pass
        self.state.update(
            state="PLAYING",
            uri=self._sm6_session_uri,
            position="0",
        )
        self.state.arm_elapsed_assume(
            elapsed_ms=0,
            delay_seconds=settings.sm6_position_assume_skip_delay_seconds,
        )
        logger.info(
            "%s SM6 SetCurrentPlaylistTrack -> %s (plex_offset=%s queue_index=%s sm6_id=%s)",
            self.dlna.name,
            getattr(track, "title", "?"),
            plex_offset,
            queue_index,
            sm6_track_id,
        )
        self._sm6_mark_playback_owned()
        self._sm6_notify_plex_timeline_sync()
        return True

    async def skip_to_track(self, key):
        self.state.update(state="TRANSITIONING")
        await self.queue.select_track_key(key)
        if self._is_sm6_renderer() and await self._sm6_try_select_playlist_track():
            return
        await self.play_selected_queue_item()

    async def seek(self, offset):
        # Update state immediately to reflect the seek position before DLNA device responds
        self.state.update(position=str(timedelta(milliseconds=offset)))
        if self._is_sm6_renderer() and self.state.state == "PLAYING":
            self.state.arm_elapsed_assume(elapsed_ms=offset)
        await self.dlna.Seek(str(timedelta(milliseconds=offset)))
        # Force a state check on next loop to sync with actual DLNA device position
        self.state.check_all_next_loop = True

    async def get_elapsed(self):
        position_info = await self.dlna.GetPositionInfo()
        if position_info is None:
            return 0
        t = position_info.RelTime
        t = parse_timedelta(t)
        return int(t.total_seconds() * 1000)

    async def get_volume(self):
        if self._is_sm6_renderer():
            level = await self._sm6_get_device_level()
            if level is None:
                return 0
            from dlna.sm6_volume import device_to_plex

            return device_to_plex(level)
        volume = await self.dlna.GetVolume()
        volume = int(volume.CurrentVolume)
        return convert_volume(volume, self.dlna.volume_max, self.dlna.volume_min, 100, 0, 1)

    async def set_volume(self, volume, *, sm6_step: int | None = None):
        if self._is_sm6_renderer():
            if self._sm6_relinquished_control:
                self.state.volume = int(volume)
                return
            if self._sm6_should_ignore_volume_write(int(volume)):
                return
            from dlna.sm6_volume import plex_to_dlna_level, step_to_dlna_level

            targets = await self._sm6_target_devices()
            if not targets:
                logger.warning("%s SM6 SetVolume: no physical target", self.dlna.name)
                return

            volume_range = await self._sm6_volume_range()
            if sm6_step is not None:
                from dlna.sm6_volume import step_to_dlna_level

                desired = step_to_dlna_level(sm6_step, volume_range)
            else:
                desired = plex_to_dlna_level(volume, volume_range)
            logger.debug(
                "%s SM6 SetVolume Plex=%s%% step=%s -> DesiredVolume=%s range=%s..%s targets=%s",
                self.dlna.name,
                volume,
                sm6_step,
                desired,
                volume_range.minimum,
                volume_range.maximum,
                [t.name for t in targets],
            )
            self.state.volume = int(volume)
            self._sm6_mark_outbound_activity(5.0)
            for device in targets:
                await self._sm6_set_volume_on_device(device, desired)
            self._sm6_schedule_volume_timeline_notify()
            return
        volume = convert_volume(
            volume, 100, 0, self.dlna.volume_max, self.dlna.volume_min, self.dlna.volume_step
        )
        await self.dlna.SetVolume(volume)
        self.state.check_all_next_loop = True

    async def is_muted(self):
        mute = await self.dlna.GetMute()
        return mute.CurrentMute

    def start_plex_tv_notify(self):
        self._plex_tv_task = asyncio.create_task(
            self._update_plex_tv_connection_loop(),
            name=f"plex_tv_notify_{self.dlna.name}"
        )

    async def _update_plex_tv_connection_loop(self):
        try:
            while True:
                try:
                    await self.update_plex_tv_connection()
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    logger.debug("Plex TV connection update timed out for %s", self.dlna.name)
                except Exception:
                    logger.exception("Unexpected error updating Plex TV connection")
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.debug("Plex TV connection loop cancelled for %s", self.dlna.name)

    async def update_plex_tv_connection(self):
        if not settings.host_ip:
            return
        if not self.plex_bind_token:
            self.plex_bind_token = settings.get_token_for_uuid(self.dlna.uuid)
            if not self.plex_bind_token:
                return
        await g.http.put(f"https://plex.tv/devices/{self.dlna.uuid}?X-Plex-Token={self.plex_bind_token}",
                         data={"Connection[][uri]": f"http://{settings.host_ip}:{settings.http_port}"},
                         headers=pms_header(self.dlna))

    def update_state(self, info):
        if info.propertyset:
            info = info.propertyset.property.LastChange.Event.InstanceID
        else:
            return
        state = info.TransportState['@val']
        uri = info.AVTransportURI['@val']
        pos = info.RelativeTimePosition['@val']
        if not state and not uri and not pos:
            logger.debug("ignoring notice no info")
            return
        if not state:
            state = ""
        if not uri:
            uri = ""
        if not pos:
            pos = ""
        if __debug__:
            logger.debug("%s update state from sub %s %s %s", self.dlna.name, state, uri, pos)
        self.state.update(state=state, uri=uri, position=pos)

    @property
    def plex_state(self):
        if self.state.state is None:
            return None
        if self.state.state == "PLAYING":
            return "playing"
        if self.state.state == "STOPPED":
            return "stopped"
        if self.state.state == "NO_MEDIA_PRESENT":
            return "stopped"
        if self.state.state == "PAUSED_PLAYBACK":
            return "paused"
        if self.state.state == "TRANSITIONING":
            return "playing"

    async def get_pms_state(self):
        if self.state is None:
            return None
        d = await self.get_state()
        if (not d or d.get("state") is None) and self.plex_state == "stopped":
            d = {"state": "stopped"}
        keys = ['state', 'ratingKey', 'key', 'time', 'duration', 'playQueueItemID', 'shuffle', 'repeat', 'containerKey']
        not_wanted_keys = []
        for k, _ in d.items():
            if k not in keys:
                not_wanted_keys.append(k)
        for k in not_wanted_keys:
            del d[k]
        for k, v in list(d.items()):
            if isinstance(v, DotMap):
                d[k] = str(v)
            elif v is not None and not isinstance(v, (str, int, float, bool)):
                d[k] = str(v)
        d['X-Plex-Token'] = self.plex_lib.token
        return d

    async def get_state(self):
        if self.state is None or self.state.state in ("STOPPED", "NO_MEDIA_PRESENT", None):
            return {}
        if self.queue is None:
            if not getattr(self, "current_track_info", None):
                return {}
            lib_info = self.plex_lib.get_info()
            track = self.current_track_info
            return {
                "state": self.plex_state,
                "time": self.state.live_elapsed_ms() if hasattr(self.state, "live_elapsed_ms") else self.state.elapsed,
                "volume": self.state.volume,
                "mute": "1" if self.state.muted else "0",
                "ratingKey": getattr(track, "ratingKey", None),
                "key": getattr(track, "key", None) or f"/library/metadata/{getattr(track, 'ratingKey', '')}",
                "duration": getattr(track, "duration", None),
                "title": getattr(track, "title", None),
                **lib_info,
            }
        lib_info = self.plex_lib.get_info()
        shuffle = self.shuffle
        if (
            shuffle > 0
            and not self._is_sm6_renderer()
            and not await self.queue.allow_shuffle()
        ):
            shuffle = 0
        if self._is_sm6_renderer() and self.current_track_info is not None:
            track_info = self._track_info_for_plex(self.current_track_info)
        else:
            track_info = await self.queue.get_track_info()
        time = self.state.live_elapsed_ms() if hasattr(self.state, "live_elapsed_ms") else self.state.elapsed
        volume = self.state.volume
        mute = "1" if self.state.muted else "0"
        state = {
            'state': self.plex_state,
            'time': time,
            'volume': volume,
            'mute': mute,
            'shuffle': shuffle,
            'repeat': self._repeat_value()
        }
        state.update(track_info)
        state.update(lib_info)
        if self._transport_state_override:
            state['state'] = 'paused' if self._active_operation_target_paused else 'playing'
            state['time'] = 0
        return state

    def __del__(self):
        state = getattr(self, "state", None)
        if state is not None:
            state._thread_should_stop = True
