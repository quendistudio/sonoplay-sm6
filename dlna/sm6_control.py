"""Cambridge Stream Magic proprietary control (QueueFolder, DeleteAll)."""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from dlna.sm6_navigator import (
    IS_REGISTERED_NAVIGATOR_NAME_ACTION,
    REGISTER_NAMED_NAVIGATOR_ACTION,
    REGISTER_NAVIGATOR_ACTION,
    build_is_registered_navigator_name_body,
    build_register_named_navigator_body,
    build_register_navigator_body,
    parse_is_registered_navigator_name,
    parse_queue_folder_result,
    parse_ret_navigator_id,
)
from dlna.sm6_queue import (
    DELETE_ALL_SOAP_ACTION,
    GET_REPEAT_SOAP_ACTION,
    GET_SHUFFLE_SOAP_ACTION,
    QUEUE_FOLDER_SOAP_ACTION,
    SET_REPEAT_SOAP_ACTION,
    SET_SHUFFLE_SOAP_ACTION,
    build_delete_all_body,
    build_get_repeat_body,
    build_get_shuffle_body,
    build_queue_folder_body,
    build_set_repeat_body,
    build_set_shuffle_body,
    parse_repeat_response,
    parse_shuffle_response,
    reciva_radio_invoke_url,
    uu_playlist_invoke_url,
)
from dlna.sm6_simple_remote import (
    KEY_INFO,
    KEY_PLAY_PAUSE,
    KEY_SKIP_NEXT,
    KEY_SKIP_PREVIOUS,
    KEY_STOP,
    SIMPLE_REMOTE_SOAP_ACTION,
    build_key_pressed_body,
    simple_remote_invoke_url,
)
from dlna.sm6_playlist import (
    GET_CURRENT_PLAYLIST_TRACK_ACTION,
    GET_MEDIA_QUEUE_INDEX_ACTION,
    GET_PLAYLIST_LENGTH_ACTION,
    GET_PLAYLIST_TRACK_DETAILS_ACTION,
    SET_CURRENT_PLAYLIST_TRACK_ACTION,
    Sm6PlaylistEntry,
    Sm6PlaylistState,
    build_get_current_playlist_track_body,
    build_get_media_queue_index_body,
    build_get_playlist_length_body,
    build_get_playlist_track_details_body,
    build_set_current_playlist_track_body,
    parse_current_playlist_track_id,
    parse_media_queue_index,
    parse_playlist_length,
    parse_playlist_track_details,
)
from dlna.sm6_playlist_edit import (
    DELETE_PLAYLIST_TRACK_ACTION,
    INSERT_PLAYLIST_TRACK_ACTION,
    MOVE_PLAYLIST_TRACK_ACTION,
    build_delete_playlist_track_body,
    build_insert_playlist_track_body,
    build_move_playlist_track_body,
)
from dlna.sm6_power import (
    GET_POWER_STATE_ACTION,
    SET_POWER_STATE_ACTION,
    build_get_power_state_body,
    build_set_power_state_body,
    parse_power_state,
)
from dlna.sm6_sources import (
    AUDIO_SOURCE_MEDIA_PLAYER,
    GET_AUDIO_SOURCE_BY_NUMBER_ACTION,
    SET_AUDIO_SOURCE_BY_NUMBER_ACTION,
    build_get_audio_source_by_number_body,
    build_set_audio_source_by_number_body,
    parse_current_audio_source_id,
)
from settings import settings
from utils import g

logger = logging.getLogger(__name__)

_SOAP_RETRIES = 3
_SOAP_RETRY_DELAY_SECONDS = 0.12
_NAVIGATOR_ID_CACHE: dict[str, str] = {}


async def sm6_transport_key_pressed(device, key: str) -> None:
    """KeyPressed via control lane (no queue preempt — play/pause/stop/skip)."""
    from dlna.sm6_dispatcher import get_sm6_dispatcher
    from dlna.sm6_rendering_control import sm6_preferred_description_url

    device_uuid = str(getattr(device, "uuid", None) or sm6_preferred_description_url(device))
    description_url = sm6_preferred_description_url(device)
    dispatcher = get_sm6_dispatcher(device_uuid, description_url)
    sm6 = Sm6Control(description_url)

    async def _send() -> None:
        await sm6._key_pressed_direct(key)

    await dispatcher.submit_control(_send, label=f"KeyPressed {key}")


class Sm6Control:
    def __init__(self, description_url: str) -> None:
        self._description_url = description_url
        self._cached_plex_navigator_id: str | None = _NAVIGATOR_ID_CACHE.get(description_url)

    def _remember_navigator_id(self, navigator_id: str) -> str:
        self._cached_plex_navigator_id = navigator_id
        _NAVIGATOR_ID_CACHE[self._description_url] = navigator_id
        return navigator_id

    async def _post(self, url: str, body: str, soap_action: str, *, label: str) -> str:
        headers = {
            "SOAPAction": soap_action,
            'Content-Type': 'text/xml; charset="utf-8"',
        }
        last_error: Exception | None = None
        for attempt in range(_SOAP_RETRIES):
            if attempt:
                await asyncio.sleep(_SOAP_RETRY_DELAY_SECONDS)
            try:
                async with g.http.post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=settings.http_timeout_dlna,
                ) as response:
                    text = await response.text()
                    if response.status != 200:
                        raise RuntimeError(f"{label} HTTP {response.status} — {text[:200]}")
                    return text
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
        raise RuntimeError(f"{label} unavailable") from last_error

    async def _navigator_name_candidates(self) -> list[str]:
        names: list[str] = []
        if settings.sm6_plex_navigator_name:
            names.append(settings.sm6_plex_navigator_name)
        try:
            from plex.dlna_browser import plex_dlna_friendly_name

            device_url = settings.resolved_plex_dlna_device_url()
            if device_url:
                friendly = await plex_dlna_friendly_name(device_url)
                if friendly:
                    names.append(friendly)
        except Exception as exc:
            logger.debug("SM6 Plex DLNA friendlyName lookup failed: %s", exc)
        names.extend(["Plex", "Plex Media Server"])
        seen: set[str] = set()
        unique: list[str] = []
        for name in names:
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(name)
        return unique

    async def _query_registered_navigator(self, name: str) -> tuple[bool, str | None]:
        xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_is_registered_navigator_name_body(name),
            IS_REGISTERED_NAVIGATOR_NAME_ACTION,
            label="IsRegisteredNavigatorName",
        )
        registered, navigator_id = parse_is_registered_navigator_name(xml)
        logger.debug(
            "SM6 IsRegisteredNavigatorName name=%r registered=%s id=%s",
            name,
            registered,
            navigator_id,
        )
        return registered, navigator_id

    async def _register_named_navigator(self, name: str) -> str | None:
        xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_register_named_navigator_body(name),
            REGISTER_NAMED_NAVIGATOR_ACTION,
            label="RegisterNamedNavigator",
        )
        navigator_id = parse_ret_navigator_id(xml)
        logger.info(
            "SM6 RegisterNamedNavigator name=%r id=%s",
            name,
            navigator_id or "?",
        )
        return navigator_id

    async def _register_anonymous_navigator(self) -> str | None:
        xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_register_navigator_body(),
            REGISTER_NAVIGATOR_ACTION,
            label="RegisterNavigator",
        )
        navigator_id = parse_ret_navigator_id(xml)
        logger.info("SM6 RegisterNavigator id=%s", navigator_id or "?")
        return navigator_id

    async def resolve_plex_navigator_id(self) -> str:
        if settings.sm6_plex_navigator_id:
            return settings.sm6_plex_navigator_id
        if self._cached_plex_navigator_id:
            return self._cached_plex_navigator_id

        for name in await self._navigator_name_candidates():
            registered, navigator_id = await self._query_registered_navigator(name)
            if registered and navigator_id:
                logger.info('SM6 Plex navigator resolved name=%r id=%s', name, navigator_id)
                return self._remember_navigator_id(navigator_id)

        if settings.sm6_plex_navigator_auto_register:
            for name in await self._navigator_name_candidates():
                navigator_id = await self._register_named_navigator(name)
                if not navigator_id:
                    continue
                registered, confirmed_id = await self._query_registered_navigator(name)
                resolved = confirmed_id or navigator_id
                if registered or resolved:
                    logger.info(
                        'SM6 Plex navigator registered name=%r id=%s',
                        name,
                        resolved,
                    )
                    return self._remember_navigator_id(resolved)

            navigator_id = await self._register_anonymous_navigator()
            if navigator_id:
                return self._remember_navigator_id(navigator_id)

        candidates = await self._navigator_name_candidates()
        raise RuntimeError(
            "Plex navigator is not registered on the SM6 and auto-register failed "
            f"(tried: {', '.join(repr(name) for name in candidates)}). "
            "Select audio source id 10 on the device and open the Plex DLNA server once, "
            "or set SM6_PLEX_NAVIGATOR_ID / SM6_PLEX_NAVIGATOR_NAME."
        )

    async def clear_queue(self) -> None:
        logger.info("SM6 DeleteAll (clear internal queue)")
        await self._post(
            uu_playlist_invoke_url(self._description_url),
            build_delete_all_body(),
            DELETE_ALL_SOAP_ACTION,
            label="DeleteAll",
        )

    async def insert_playlist_track(self, *, insert_position: int, didl: str) -> None:
        logger.info("SM6 InsertPlaylistTrack position=%s", insert_position)
        await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_insert_playlist_track_body(
                insert_position=insert_position,
                didl=didl,
            ),
            INSERT_PLAYLIST_TRACK_ACTION,
            label="InsertPlaylistTrack",
        )

    async def delete_playlist_track(self, *, playlist_track_id: int) -> None:
        logger.info("SM6 DeletePlaylistTrack id=%s", playlist_track_id)
        await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_delete_playlist_track_body(playlist_track_id=playlist_track_id),
            DELETE_PLAYLIST_TRACK_ACTION,
            label="DeletePlaylistTrack",
        )

    async def move_playlist_track(self, *, from_index: int, to_index: int) -> None:
        logger.info("SM6 MovePlaylistTrack from=%s to=%s", from_index, to_index)
        await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_move_playlist_track_body(from_index=from_index, to_index=to_index),
            MOVE_PLAYLIST_TRACK_ACTION,
            label="MovePlaylistTrack",
        )

    async def queue_folder(
        self,
        didl: str,
        *,
        action: str,
        server_udn: str,
        navigator_id: str | None = None,
    ) -> None:
        resolved = navigator_id or await self.resolve_plex_navigator_id()
        body = build_queue_folder_body(
            didl=didl,
            action=action,
            server_udn=server_udn,
            navigator_id=resolved,
        )
        logger.info(
            "SM6 QueueFolder action=%s server_udn=%s navigator_id=%s",
            action,
            server_udn,
            resolved,
        )
        xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            body,
            QUEUE_FOLDER_SOAP_ACTION,
            label="QueueFolder",
        )
        result = parse_queue_folder_result(xml)
        if result == "BAD_NAVIGATOR":
            self._cached_plex_navigator_id = None
            _NAVIGATOR_ID_CACHE.pop(self._description_url, None)
            raise RuntimeError(
                f"QueueFolder: Plex navigator id={resolved!r} not recognized by the SM6"
            )
        if result != "OK":
            raise RuntimeError(f"QueueFolder: {result or 'missing Result'}")

    async def key_pressed(self, key: str) -> None:
        await self._key_pressed_direct(key)

    async def _key_pressed_direct(self, key: str) -> None:
        logger.info("SM6 KeyPressed key=%s url=%s", key, self._description_url)
        await self._post(
            simple_remote_invoke_url(self._description_url),
            build_key_pressed_body(key),
            SIMPLE_REMOTE_SOAP_ACTION,
            label=f"KeyPressed {key}",
        )

    async def play_pause(self) -> None:
        await self.key_pressed(KEY_PLAY_PAUSE)

    async def stop(self) -> None:
        await self.key_pressed(KEY_STOP)

    async def skip_next(self) -> None:
        await self.key_pressed(KEY_SKIP_NEXT)

    async def skip_previous(self) -> None:
        await self.key_pressed(KEY_SKIP_PREVIOUS)

    async def _read_power_state_unlocked(self) -> str | None:
        xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_get_power_state_body(),
            GET_POWER_STATE_ACTION,
            label="GetPowerState",
        )
        return parse_power_state(xml)

    async def get_power_state(self) -> str | None:
        return await self._read_power_state_unlocked()

    async def ensure_power_on(self) -> None:
        """Wake the SM6 from IDLE when Plex connects or starts playback."""
        state = await self._read_power_state_unlocked()
        if state != "IDLE":
            return
        logger.info("SM6 SetPowerState ON (was IDLE)")
        await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_set_power_state_body("ON"),
            SET_POWER_STATE_ACTION,
            label="SetPowerState ON",
        )

    async def _read_current_audio_source_id(self) -> int | None:
        xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_get_audio_source_by_number_body(),
            GET_AUDIO_SOURCE_BY_NUMBER_ACTION,
            label="GetAudioSourceByNumber",
        )
        return parse_current_audio_source_id(xml)

    async def get_current_audio_source_id(self) -> int | None:
        return await self._read_current_audio_source_id()

    async def ensure_media_player_source(self) -> None:
        """Switch to UPnP push audio source (id 10, AUDIO_SOURCE_MEDIA_PLAYER).

        Without this, AVTransport reflects the native SM6 Plex browser state
        (first track in the DLNA library), not the QueueFolder queue.
        """
        current = await self._read_current_audio_source_id()
        if current == AUDIO_SOURCE_MEDIA_PLAYER:
            logger.debug("SM6 already on Media Player source (%s)", AUDIO_SOURCE_MEDIA_PLAYER)
            return
        logger.info(
            "SM6 switching audio source %s -> Media Player (%s)",
            current,
            AUDIO_SOURCE_MEDIA_PLAYER,
        )
        await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_set_audio_source_by_number_body(AUDIO_SOURCE_MEDIA_PLAYER),
            SET_AUDIO_SOURCE_BY_NUMBER_ACTION,
            label="SetAudioSourceByNumber",
        )
        await self._post(
            simple_remote_invoke_url(self._description_url),
            build_key_pressed_body(KEY_INFO),
            SIMPLE_REMOTE_SOAP_ACTION,
            label="KeyPressed INFO",
        )

    async def get_playlist_length(self) -> int:
        xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_get_playlist_length_body(),
            GET_PLAYLIST_LENGTH_ACTION,
            label="GetPlaylistLength",
        )
        return parse_playlist_length(xml)

    async def get_media_queue_index(self) -> int:
        xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_get_media_queue_index_body(),
            GET_MEDIA_QUEUE_INDEX_ACTION,
            label="GetMediaQueueIndex",
        )
        return parse_media_queue_index(xml)

    async def get_current_playlist_track_id(self) -> int:
        xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_get_current_playlist_track_body(),
            GET_CURRENT_PLAYLIST_TRACK_ACTION,
            label="GetCurrentPlaylistTrack",
        )
        return parse_current_playlist_track_id(xml)

    async def get_current_queue_position(self) -> tuple[int, int]:
        """Read current track and queue index (parallel SOAP requests)."""
        index_xml, current_xml = await asyncio.gather(
            self._post(
                reciva_radio_invoke_url(self._description_url),
                build_get_media_queue_index_body(),
                GET_MEDIA_QUEUE_INDEX_ACTION,
                label="GetMediaQueueIndex",
            ),
            self._post(
                reciva_radio_invoke_url(self._description_url),
                build_get_current_playlist_track_body(),
                GET_CURRENT_PLAYLIST_TRACK_ACTION,
                label="GetCurrentPlaylistTrack",
            ),
        )
        return (
            parse_current_playlist_track_id(current_xml),
            parse_media_queue_index(index_xml),
        )

    async def set_current_playlist_track(self, track_id: int) -> None:
        logger.info("SM6 SetCurrentPlaylistTrack track_id=%s", track_id)
        await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_set_current_playlist_track_body(track_id=track_id),
            SET_CURRENT_PLAYLIST_TRACK_ACTION,
            label="SetCurrentPlaylistTrack",
        )

    async def get_playlist_track_details(
        self,
        *,
        start_track_id: int = 0,
        track_count: int | None = None,
    ) -> list[Sm6PlaylistEntry]:
        count = track_count if track_count is not None else max(1, await self._get_playlist_length_unlocked())
        xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_get_playlist_track_details_body(
                start_track_id=start_track_id,
                track_count=count,
            ),
            GET_PLAYLIST_TRACK_DETAILS_ACTION,
            label="GetPlaylistTrackDetails",
        )
        return parse_playlist_track_details(xml)

    async def _get_playlist_length_unlocked(self) -> int:
        xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_get_playlist_length_body(),
            GET_PLAYLIST_LENGTH_ACTION,
            label="GetPlaylistLength",
        )
        return parse_playlist_length(xml)

    async def read_playlist_state(self, *, fetch_tracks: bool = True) -> Sm6PlaylistState:
        """Full SM6 queue state (position + track details)."""
        length = await self._get_playlist_length_unlocked()
        index_xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_get_media_queue_index_body(),
            GET_MEDIA_QUEUE_INDEX_ACTION,
            label="GetMediaQueueIndex",
        )
        current_xml = await self._post(
            reciva_radio_invoke_url(self._description_url),
            build_get_current_playlist_track_body(),
            GET_CURRENT_PLAYLIST_TRACK_ACTION,
            label="GetCurrentPlaylistTrack",
        )
        media_queue_index = parse_media_queue_index(index_xml)
        current_track_id = parse_current_playlist_track_id(current_xml)
        tracks: list[Sm6PlaylistEntry] = []
        if fetch_tracks and length > 0:
            details_xml = await self._post(
                reciva_radio_invoke_url(self._description_url),
                build_get_playlist_track_details_body(
                    start_track_id=0,
                    track_count=length,
                ),
                GET_PLAYLIST_TRACK_DETAILS_ACTION,
                label="GetPlaylistTrackDetails",
            )
            tracks = parse_playlist_track_details(details_xml)
            track_ids = {entry.track_id for entry in tracks}
            if current_track_id not in track_ids:
                current_xml = await self._post(
                    reciva_radio_invoke_url(self._description_url),
                    build_get_playlist_track_details_body(
                        start_track_id=current_track_id,
                        track_count=1,
                    ),
                    GET_PLAYLIST_TRACK_DETAILS_ACTION,
                    label="GetPlaylistTrackDetails",
                )
                current_entries = parse_playlist_track_details(current_xml)
                if current_entries:
                    merged = {entry.track_id: entry for entry in tracks}
                    for entry in current_entries:
                        merged[entry.track_id] = entry
                    ordered: list[Sm6PlaylistEntry] = list(tracks)
                    known = {entry.track_id for entry in tracks}
                    for entry in current_entries:
                        if entry.track_id in known:
                            continue
                        insert_at = media_queue_index
                        if 0 <= insert_at <= len(ordered):
                            ordered.insert(insert_at, entry)
                        else:
                            ordered.append(entry)
                    tracks = ordered
                    logger.info(
                        "SM6 playlist: merged missing current track_id=%s (%s) at index=%s",
                        current_track_id,
                        current_entries[0].title,
                        media_queue_index,
                    )
        return Sm6PlaylistState(
            length=length,
            current_track_id=current_track_id,
            media_queue_index=media_queue_index,
            tracks=tuple(tracks),
        )

    async def get_shuffle(self) -> bool:
        xml = await self._post(
            uu_playlist_invoke_url(self._description_url),
            build_get_shuffle_body(),
            GET_SHUFFLE_SOAP_ACTION,
            label="Shuffle",
        )
        return parse_shuffle_response(xml)

    async def get_repeat(self) -> bool:
        xml = await self._post(
            uu_playlist_invoke_url(self._description_url),
            build_get_repeat_body(),
            GET_REPEAT_SOAP_ACTION,
            label="Repeat",
        )
        return parse_repeat_response(xml)

    async def set_shuffle(self, enabled: bool) -> None:
        logger.info("SM6 SetShuffle %s", "on" if enabled else "off")
        await self._post(
            uu_playlist_invoke_url(self._description_url),
            build_set_shuffle_body(enabled),
            SET_SHUFFLE_SOAP_ACTION,
            label="SetShuffle",
        )

    async def set_repeat(self, enabled: bool) -> None:
        logger.info("SM6 SetRepeat %s", "on" if enabled else "off")
        await self._post(
            uu_playlist_invoke_url(self._description_url),
            build_set_repeat_body(enabled),
            SET_REPEAT_SOAP_ACTION,
            label="SetRepeat",
        )
