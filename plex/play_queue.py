import logging

from dotmap import DotMap
from starlette.datastructures import URL, QueryParams
import math

logger = logging.getLogger(__name__)

from utils import g

UNLIMITED = math.inf

MIN_QUEUE_GAP = 25


class PlayQueue(object):

    @classmethod
    def from_url(cls, url):
        from plex.adapters import PlexLib
        url = URL(url)
        plex_lib = PlexLib()
        plex_lib.protocol = url.scheme
        plex_lib.address = url.hostname
        plex_lib.port = url.port
        q = QueryParams(url.query)
        plex_lib.token = q.get("X-Plex-Token")
        return PlayQueue(url.path + "?" + url.remove_query_params("X-Plex-Token").query,
                         plex_lib)

    def __init__(self, container_key, plex_lib):
        self.container_key = container_key
        self.plex_lib = plex_lib
        self.info = None
        self.start_offset = None
        self.repeat = 0

    @classmethod
    async def _ensure_machine_id(cls, plex_lib) -> None:
        if plex_lib.machine_id:
            return
        url = plex_lib.build_url("/identity")
        async with g.http.get(url, headers=plex_lib.request_headers(accept_json=True)) as res:
            res.raise_for_status()
            payload = await res.json()
        machine_id = (payload.get("MediaContainer") or {}).get("machineIdentifier")
        if machine_id:
            plex_lib.machine_id = machine_id

    @classmethod
    def _rating_key_uri(cls, plex_lib, rating_key: str) -> str:
        if plex_lib.machine_id:
            return (
                f"server://{plex_lib.machine_id}/com.plexapp.plugins.library"
                f"/library/metadata/{rating_key}"
            )
        return f"/library/metadata/{rating_key}"

    @classmethod
    async def create_from_rating_keys(
        cls,
        plex_lib,
        rating_keys: list[str],
        *,
        selected_index: int = 0,
        shuffle: int = 0,
        repeat: int = 0,
        sequential: bool = False,
    ) -> "PlayQueue":
        """Create a Plex playQueue from library rating keys (SM6 → Plex sync)."""
        from urllib.parse import quote_plus, urlencode

        if not rating_keys:
            raise ValueError("rating_keys must not be empty")
        selected_index = max(0, min(int(selected_index), len(rating_keys) - 1))
        has_duplicates = len(rating_keys) != len(set(rating_keys))
        if sequential or has_duplicates:
            return await cls._create_from_rating_keys_sequential(
                plex_lib,
                rating_keys,
                selected_index=selected_index,
                shuffle=shuffle,
                repeat=repeat,
            )

        await cls._ensure_machine_id(plex_lib)
        selected_key = rating_keys[selected_index]

        if len(rating_keys) == 1:
            uri = cls._rating_key_uri(plex_lib, rating_keys[0])
        else:
            keys_csv = ",".join(rating_keys)
            uri = f"library:///directory/{quote_plus(f'/library/metadata/{keys_csv}')}"

        query: list[tuple[str, str | int]] = [
            ("type", "audio"),
            ("shuffle", int(shuffle)),
            ("repeat", int(repeat)),
            ("continuous", 0),
            ("includeRelated", 0),
            ("includeChapters", 0),
            ("uri", uri),
            ("key", f"/library/metadata/{selected_key}"),
        ]
        path = f"/playQueues?{urlencode(query)}"
        url = plex_lib.build_url(path)
        logger.info(
            "create playQueue from %d rating keys (selected=%s)",
            len(rating_keys),
            selected_index,
        )
        headers = plex_lib.request_headers(accept_json=True)
        async with g.http.post(url, headers=headers) as res:
            if res.status >= 400:
                body = await res.text()
                logger.warning(
                    "playQueue create failed (%s): %s",
                    res.status,
                    body[:500] if body else res.reason,
                )
            res.raise_for_status()
            info = DotMap((await res.json())["MediaContainer"])
        container_key = f"/playQueues/{info.playQueueID}?playQueueID={info.playQueueID}"
        queue = cls(container_key, plex_lib)
        queue.info = info
        queue.repeat = int(repeat)
        for idx, track in enumerate(info.Metadata):
            if track.playQueueItemID == info.playQueueSelectedItemID:
                queue.start_offset = info.playQueueSelectedItemOffset - idx
                break
        plex_total = int(getattr(info, "playQueueTotalCount", 0) or len(info.Metadata))
        if plex_total != len(rating_keys):
            logger.info(
                "playQueue bulk create returned %d items for %d keys — rebuilding sequentially",
                plex_total,
                len(rating_keys),
            )
            return await cls._create_from_rating_keys_sequential(
                plex_lib,
                rating_keys,
                selected_index=selected_index,
                shuffle=shuffle,
                repeat=repeat,
            )
        if info.playQueueSelectedItemOffset != selected_index:
            await queue.set_selected_offset(selected_index)
        return queue

    @classmethod
    async def _create_from_rating_keys_sequential(
        cls,
        plex_lib,
        rating_keys: list[str],
        *,
        selected_index: int,
        shuffle: int,
        repeat: int,
    ) -> "PlayQueue":
        """Build playQueue one item at a time (preserves duplicates and multi-album order)."""
        from urllib.parse import quote, urlencode

        await cls._ensure_machine_id(plex_lib)
        first_key = rating_keys[0]
        query: list[tuple[str, str | int]] = [
            ("type", "audio"),
            ("shuffle", int(shuffle)),
            ("repeat", int(repeat)),
            ("continuous", 0),
            ("includeRelated", 0),
            ("includeChapters", 0),
            ("uri", cls._rating_key_uri(plex_lib, first_key)),
            ("key", f"/library/metadata/{first_key}"),
        ]
        path = f"/playQueues?{urlencode(query)}"
        url = plex_lib.build_url(path)
        logger.info(
            "create playQueue sequentially from %d rating keys (selected=%s)",
            len(rating_keys),
            selected_index,
        )
        headers = plex_lib.request_headers(accept_json=True)
        async with g.http.post(url, headers=headers) as res:
            res.raise_for_status()
            info = DotMap((await res.json())["MediaContainer"])
        container_key = f"/playQueues/{info.playQueueID}?playQueueID={info.playQueueID}"
        queue = cls(container_key, plex_lib)
        queue.info = info
        queue.repeat = int(repeat)
        queue.start_offset = 0
        for rating_key in rating_keys[1:]:
            await queue.add_item_by_rating_key(rating_key)
        await queue.set_selected_offset(selected_index)
        return queue

    async def add_item_by_rating_key(self, rating_key: str) -> None:
        """Append one library track to this playQueue (party-mode / Up Next)."""
        from urllib.parse import urlencode

        await self.get_info()
        uri = self._rating_key_uri(self.plex_lib, rating_key)
        path = f"/playQueues/{self.info.playQueueID}?{urlencode({'uri': uri})}"
        url = self.plex_lib.build_url(path)
        async with g.http.put(url, headers=self.plex_lib.request_headers(accept_json=True)) as res:
            if res.status >= 400:
                body = await res.text()
                logger.warning(
                    "playQueue add item failed (%s) key=%s: %s",
                    res.status,
                    rating_key,
                    body[:300] if body else res.reason,
                )
            res.raise_for_status()
            self.info = DotMap((await res.json())["MediaContainer"])

    def _fetch_key(self) -> str:
        # Strip `own=1` — Plex enforces client-id ownership which won't match ours
        key = URL("http://x" + self.container_key).remove_query_params("own")
        return key.path + ("?" + key.query if key.query else "")

    async def get_info(self):
        if self.info is None:
            url = self.plex_lib.build_url(self._fetch_key())
            logger.debug("get queue %s", url)
            async with g.http.get(url, headers=self.plex_lib.request_headers(accept_json=True)) as res:
                res.raise_for_status()
                self.info = DotMap((await res.json())['MediaContainer'])
                for idx, track in enumerate(await self.available_tracks()):
                    if track.playQueueItemID == await self.selected_item_id():
                        self.start_offset = await self.selected_offset() - idx
                        break
        return self.info

    async def refresh_queue(self, playQueueID):
        if playQueueID != self.info.playQueueID:
            logger.debug("refresh to a different queue? %s -> %s", self.info.playQueueID, playQueueID)
            self.container_key = str(self.container_key).replace(str(self.info.playQueueID), str(playQueueID), 1)
        old_selected_item_id = await self.selected_item_id()
        old_selected_item_offset = await self.selected_offset()
        url = self.plex_lib.build_url(self._fetch_key())
        logger.debug("refresh queue from %s", url)
        async with g.http.get(url, headers=self.plex_lib.request_headers(accept_json=True)) as res:
            res.raise_for_status()
            info = DotMap((await res.json())['MediaContainer'])
            logger.debug(
                "refresh queue raw selected id/offset %s %s total %s metadata %s "
                "shuffled=%s",
                info.playQueueSelectedItemID,
                info.playQueueSelectedItemOffset,
                info.playQueueTotalCount,
                len(info.Metadata),
                getattr(info, "playQueueShuffled", "?"),
            )
            found = 0
            new_available_offset = None
            start_offset = None
            for idx, track in enumerate(info.Metadata):
                if track.playQueueItemID == old_selected_item_id:
                    new_available_offset = idx
                    found += 1
                if track.playQueueItemID == info.playQueueSelectedItemID:
                    start_offset = info.playQueueSelectedItemOffset - idx
                    found += 1
                if found >= 2:
                    break
            if new_available_offset is None or start_offset is None:
                raise Exception("refreshed queue has no current selected item?")
            selected_offset = new_available_offset + start_offset
            logger.debug(
                "refreshed queue mapping oldOffset %s -> %s localStart %s -> %s "
                "newAvailableOffset %s rawSelectedOffset %s rawStartOffset %s",
                old_selected_item_offset, selected_offset,
                self.start_offset, start_offset,
                new_available_offset,
                info.playQueueSelectedItemOffset,
                start_offset
            )
        info.playQueueSelectedItemID = old_selected_item_id
        info.playQueueSelectedItemOffset = selected_offset
        self.info = info
        self.start_offset = start_offset

    async def set_selected_offset(self, offset):
        total = await self.total_count()
        if not (0 <= offset < total):
            raise ValueError(f"Queue offset {offset} out of range [0, {total})")
        await self.get_info()

        while True:
            last_offset = self.last_offset
            if last_offset is not None and offset > last_offset - MIN_QUEUE_GAP and last_offset + 1 < total:
                expanded = await self.more(after=True)
                if not expanded:
                    break
                continue
            if self.start_offset is not None and offset < self.start_offset + MIN_QUEUE_GAP and self.start_offset > 0:
                expanded = await self.more(after=False)
                if not expanded:
                    break
                continue
            break

        info = await self.get_info()
        selected_track = await self.track(offset)
        info.playQueueSelectedItemOffset = offset
        info.playQueueSelectedItemID = selected_track.playQueueItemID

    async def track(self, offset):
        if self.info is None:
            await self.get_info()
        total = await self.total_count()
        if not (0 <= offset < total):
            raise ValueError(f"Queue offset {offset} out of range [0, {total})")

        while True:
            last_offset = self.last_offset
            if last_offset is not None and offset > last_offset:
                if not await self.more(after=True):
                    raise IndexError(f"play queue cannot move forward to offset {offset}; last_offset={last_offset}")
                continue
            if self.start_offset is not None and offset < self.start_offset:
                if not await self.more(after=False):
                    raise IndexError(f"play queue cannot move backward to offset {offset}; start_offset={self.start_offset}")
                continue
            break

        local_offset = offset - (self.start_offset or 0)
        tracks = await self.available_tracks()
        return tracks[local_offset]

    async def selected_track(self):
        return await self.track(await self.selected_offset())

    async def prev_track(self):
        return await self.next_track(reverse=True)

    async def next_track(self, reverse=False):
        direction = -1 if reverse else 1
        return await self.track(await self.selected_offset() + direction)

    def _track_matches_key(self, track, key: str) -> bool:
        if track.key == key:
            return True
        rating_from_key = key.rstrip("/").rsplit("/", 1)[-1]
        return rating_from_key.isdigit() and str(getattr(track, "ratingKey", "")) == rating_from_key

    async def select_track_key(self, key) -> bool:
        tracks = await self.available_tracks()
        for idx, track in enumerate(tracks):
            if self._track_matches_key(track, key):
                offset = idx + (self.start_offset or 0)
                await self.set_selected_offset(offset)
                logger.info(
                    "queue selected offset=%s key=%s title=%s ratingKey=%s",
                    offset,
                    key,
                    getattr(track, "title", "?"),
                    getattr(track, "ratingKey", "?"),
                )
                return True
        logger.warning(
            "no queue track matched key=%s (searched %d loaded tracks, start_offset=%s)",
            key,
            len(tracks),
            self.start_offset,
        )
        return False

    @staticmethod
    def _transcode_profile_extra(
        *,
        protocol: str,
        container: str,
        audio_codec: str,
        target_kbps: int,
    ) -> str:
        """Plex client profile override (see PlexKit TranscodeAudio)."""
        return (
            f"add-transcode-target(type=musicProfile&context=streaming&protocol={protocol}"
            f"&container={container}&audioCodec={audio_codec})"
            f"+add-limitation(scope=musicCodec&scopeName={audio_codec}&type=upperBound"
            f"&name=audio.bitrate&value={target_kbps}&replace=true)"
        )

    def build_plex_hls_transcode_url(self, rating_key: str) -> str:
        """
        Plex universal transcode URL (HLS manifest, MP3 inside MPEG-TS).

        Plex PMS rejects ``start.mp3`` / ``protocol=http`` with 400
        (``unable to find a matching profile``). Requires Generic profile.
        """
        from urllib.parse import quote
        import uuid
        from settings import settings

        key = str(rating_key).strip()
        if not key.isdigit():
            raise ValueError("ratingKey required for transcode URL")

        stream_protocol = "hls"
        manifest_extension = "m3u8"
        profile_container = "mpegts"
        audio_codec = "mp3"
        target_kbps = settings.audio_transcode_proxy_kbps

        encoded_path = quote(f"/library/metadata/{key}", safe="")
        session_id = str(uuid.uuid4())

        client_id = getattr(self.plex_lib, "client_identifier", None)
        if not client_id and hasattr(self.plex_lib, "device") and self.plex_lib.device:
            client_id = getattr(self.plex_lib.device, "uuid", None)
        if not client_id:
            client_id = "sonoplay-default"

        profile_extra = self._transcode_profile_extra(
            protocol=stream_protocol,
            container=profile_container,
            audio_codec=audio_codec,
            target_kbps=target_kbps,
        )

        query_parts = [
            f"path={encoded_path}",
            f"session={session_id}",
            f"protocol={stream_protocol}",
            "directPlay=0",
            "directStream=0",
            "directStreamAudio=1",
            "mediaIndex=0",
            "partIndex=0",
            f"maxAudioBitrate={target_kbps}",
            "mediaBufferSize=12288",
            "hasMDE=1",
            "location=lan",
            "X-Plex-Client-Profile-Name=Generic",
            f"X-Plex-Client-Profile-Extra={quote(profile_extra, safe='')}",
            f"X-Plex-Platform={quote(str(settings.platform or 'Linux'))}",
            f"X-Plex-Product={quote(str(settings.product or 'SonoPlay'))}",
            f"X-Plex-Version={quote(str(settings.version or '1'))}",
            f"X-Plex-Client-Identifier={quote(str(client_id))}",
        ]
        device_name = getattr(getattr(self.plex_lib, "device", None), "name", None)
        if device_name:
            query_parts.append(f"X-Plex-Device-Name={quote(str(device_name))}")

        query = "&".join(query_parts)
        base_path = f"/music/:/transcode/universal/start.{manifest_extension}"

        return self.plex_lib.build_url(f"{base_path}?{query}")

    def build_transcode_url(self, track):
        """Plex HLS transcode URL for a track (server-side / ffmpeg input)."""
        rating_key = self._plex_attr(track, "ratingKey")
        if not rating_key:
            raise ValueError("track ratingKey required for transcode URL")
        return self.build_plex_hls_transcode_url(str(rating_key))

    def build_sm6_transcode_proxy_url(self, track) -> str:
        """SonoPlay MP3 stream URL for SM6 QueueFolder ``<res>``."""
        rating_key = self._plex_attr(track, "ratingKey")
        if not rating_key:
            raise ValueError("track ratingKey required for SM6 transcode proxy URL")
        from plex.transcode_stream import build_sm6_transcode_proxy_url

        return build_sm6_transcode_proxy_url(str(rating_key))

    async def url_for_track(self, track, force_transcode=False, dlna_device=None):
        """
        Get the URL for a track, using transcoding if needed.
        """
        from plex.device_profiles import needs_plex_dlna_stream_url

        if force_transcode:
            logger.info("Using Plex transcode for high-bitrate track: %s", getattr(track, 'title', 'Unknown'))
            return self.build_transcode_url(track)

        if dlna_device and needs_plex_dlna_stream_url(dlna_device):
            from plex.url_resolver import get_url_resolver
            resolver = get_url_resolver()
            try:
                url = await resolver.resolve_stream_url(track)
            except LookupError as exc:
                logger.error(
                    "Plex DLNA :32469 resolution failed for %s (ratingKey=%s): %s",
                    getattr(track, "title", "?"),
                    getattr(track, "ratingKey", "?"),
                    exc,
                )
                raise
            logger.info(
                "Resolved Plex DLNA :32469 URL for %s (ratingKey=%s): %s",
                getattr(track, "title", "?"),
                getattr(track, "ratingKey", "?"),
                url,
            )
            return url

        return self.plex_lib.build_url(track.Media[0].Part[0].key)

    def _int_or_none(self, value):
        if value is None:
            return None
        from dotmap import DotMap

        if isinstance(value, (DotMap, dict, list, tuple)):
            return None
        try:
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _plex_attr(obj, name, default=None):
        """Read Plex metadata without DotMap auto-vivifying missing keys."""
        from dotmap import DotMap

        if obj is None:
            return default
        if isinstance(obj, DotMap):
            if name not in obj:
                return default
            value = obj[name]
        else:
            value = getattr(obj, name, default)
        if isinstance(value, DotMap) and not value:
            return default
        return value

    def _media_playability_values(self, media):
        bitrate = self._int_or_none(self._plex_attr(media, "bitrate"))
        sample_rate = self._int_or_none(self._plex_attr(media, "audioSampleRate"))
        if bitrate is not None or sample_rate is not None:
            return bitrate, sample_rate

        part = self._plex_attr(media, "Part")
        if not part:
            return None, None
        part0 = part[0] if isinstance(part, list) else part
        streams = self._plex_attr(part0, "Stream")
        if not streams:
            return None, None
        stream0 = streams[0] if isinstance(streams, list) else streams
        bitrate = self._int_or_none(
            self._plex_attr(stream0, "bitrate")
            or self._plex_attr(stream0, "requiredBandwidth")
        )
        sample_rate = self._int_or_none(
            self._plex_attr(stream0, "samplingRate")
            or self._plex_attr(stream0, "audioSampleRate")
        )
        return bitrate, sample_rate

    def _track_media(self, track):
        media = self._plex_attr(track, "Media")
        if media:
            return media[0] if isinstance(media, list) else media
        return None

    async def track_needs_transcode(self, track) -> bool:
        """True when track exceeds configured bitrate/sample-rate limits."""
        media = self._track_media(track)
        if media:
            bitrate, sample_rate = self._media_playability_values(media)
            if bitrate is not None or sample_rate is not None:
                return not self.is_track_playable(track)

        rating_key = self._plex_attr(track, "ratingKey")
        if not rating_key:
            return False
        full = await self.plex_lib.fetch_metadata(f"/library/metadata/{rating_key}")
        if full is None:
            return False
        return not self.is_track_playable(full)

    def is_track_playable(self, track):
        """
        Check if a track is playable based on bitrate/sample rate thresholds.
        Returns True if playable, False if it should be skipped.
        """
        from settings import settings
        
        # Get configured thresholds
        threshold_kbps = settings.audio_transcode_threshold_kbps
        sample_limit_hz = settings.audio_transcode_max_sample_rate_hz
        
        # If no thresholds configured, allow everything
        if not threshold_kbps and not sample_limit_hz:
            return True
        
        try:
            # Extract media info
            media = self._track_media(track)
            if not media:
                return True  # Unknown until enriched via track_needs_transcode()

            bitrate, sample_rate = self._media_playability_values(media)
            if bitrate is None and sample_rate is None:
                return True  # Unknown until enriched via track_needs_transcode()

            if bitrate is not None and threshold_kbps and bitrate > threshold_kbps:
                return False

            if sample_rate is not None and sample_limit_hz and sample_rate > sample_limit_hz:
                return False

            return True
        except Exception as exc:
            logger.debug("is_track_playable fallback playable=True: %s", exc)
            return True  # On error, assume playable

    async def snapshot_item_ids(self) -> tuple[set, int, int | None]:
        """Loaded IDs, selected offset, and current playQueueItemID."""
        await self.get_info()
        item_ids = {track.playQueueItemID for track in self.info.Metadata}
        offset = await self.selected_offset()
        selected_id = await self.selected_item_id()
        return item_ids, offset, selected_id

    async def allow_shuffle(self):
        info = await self.get_info()
        if info.get("allowShuffle", None) is None:
            if (await self.total_count()) == UNLIMITED:
                return False
            return True
        return info.allowShuffle

    @property
    def last_offset(self):
        if self.start_offset is None:
            return None
        return self.start_offset + len(self.info.Metadata) - 1

    async def more(self, after=True):
        if self.info is None:
            await self.get_info()
        url = URL(self.plex_lib.build_url(self.container_key))
        url = url.remove_query_params(["center", "includeBefore", "includeAfter"])
        args = {'includeAfter': 0, 'includeBefore': 0}
        if after:
            last_offset = self.last_offset
            total = await self.total_count()
            if last_offset is None or last_offset >= total - 1:
                return False
            args['includeAfter'] = 1
            t = await self.track(self.start_offset + (await self.available_count()) - 1)
            args['center'] = t.playQueueItemID
        else:
            if self.start_offset is None or self.start_offset <= 1:
                return False
            args['includeBefore'] = 1
            t = await self.track(self.start_offset)
            args['center'] = t.playQueueItemID
        url = url.include_query_params(**args)
        async with g.http.get(str(url), headers=self.plex_lib.request_headers(accept_json=True)) as res:
            res.raise_for_status()
            info = DotMap((await res.json())['MediaContainer'])
            new_items = list(getattr(info, 'Metadata', []))
            if after:
                self.info.Metadata += new_items
                logger.debug("queue %s append %d items", self.container_key, len(new_items))
            else:
                self.info.Metadata = new_items + self.info.Metadata
                logger.debug("queue %s prepend %d items", self.container_key, len(new_items))
                self.start_offset = max(0, self.start_offset - len(new_items))
        return len(new_items) > 0

    async def available_tracks(self):
        info = await self.get_info()
        return info.Metadata

    async def available_count(self):
        return len(await self.available_tracks())

    async def total_count(self):
        info = await self.get_info()
        if not info.playQueueTotalCount:
            return UNLIMITED
        return info.playQueueTotalCount

    async def selected_item_id(self):
        info = await self.get_info()
        return info.playQueueSelectedItemID

    async def selected_offset(self):
        info = await self.get_info()
        return info.playQueueSelectedItemOffset

    async def select_track_by_metadata(
        self,
        *,
        title: str,
        artist: str | None = None,
        album: str | None = None,
    ) -> bool:
        """Select a track by metadata (SM6 queue → Plex playQueue sync)."""
        await self.get_info()
        title_cf = str(title).casefold()
        artist_cf = str(artist).casefold() if artist else None
        album_cf = str(album).casefold() if album else None
        total = await self.total_count()
        if math.isinf(total):
            total = await self.available_count()
        for offset in range(int(total)):
            try:
                track = await self.track(offset)
            except (IndexError, ValueError):
                break
            if str(getattr(track, "title", "")).casefold() != title_cf:
                continue
            if album_cf and str(getattr(track, "parentTitle", "")).casefold() != album_cf:
                continue
            if artist_cf and str(getattr(track, "grandparentTitle", "")).casefold() != artist_cf:
                continue
            await self.set_selected_offset(offset)
            logger.info(
                "queue matched metadata offset=%s title=%s artist=%s album=%s ratingKey=%s",
                offset,
                title,
                artist,
                album,
                getattr(track, "ratingKey", "?"),
            )
            return True
        return False

    async def get_track_info(self):
        track = await self.selected_track()
        info = {
            'duration': track.duration,
            'key': track.key,
            'ratingKey': track.ratingKey,
            'containerKey': f"/playQueues/{self.info.playQueueID}",
            'playQueueID': self.info.playQueueID,
            'playQueueVersion': self.info.playQueueVersion,
            'playQueueItemID': track.playQueueItemID,
            # Extended metadata for UI
            'title': getattr(track, 'title', None),
            'artist': getattr(track, 'grandparentTitle', None),
            'album': getattr(track, 'parentTitle', None),
            'thumb': getattr(track, 'thumb', None),
            'art': getattr(track, 'art', None),
            'grandparentThumb': getattr(track, 'grandparentThumb', None),
            'parentThumb': getattr(track, 'parentThumb', None),
        }
        return info

