"""SM6 passive sync helpers (ratingKey lookup from polled URIs)."""

from __future__ import annotations



import math



from plex.dlna_stream_cache import (

    is_plex_dlna_stream_uri,

    object_id_from_stream_url,

    rating_key_from_pms_uri,

    rating_key_from_query_param,

    rating_key_from_sonoplay_transcode_object,

    rating_key_from_uri,

)





def sm6_entry_matches_track(entry, track) -> bool:

    """True when an SM6 playlist entry aligns with Plex track metadata."""

    if str(getattr(entry, "title", "") or "").casefold() != str(

        getattr(track, "title", "") or ""

    ).casefold():

        return False

    entry_artist = getattr(entry, "artist", None)

    if entry_artist:

        track_artist = getattr(track, "grandparentTitle", None)

        if track_artist and str(entry_artist).casefold() != str(track_artist).casefold():

            return False

    entry_album = getattr(entry, "album", None)

    if entry_album:

        track_album = getattr(track, "parentTitle", None)

        if track_album and str(entry_album).casefold() != str(track_album).casefold():

            return False

    return True





def is_plex_resolvable_uri(uri: str | None) -> bool:

    """True when TrackURI can map to Plex content (embedded key, DLNA object, or cache)."""

    return is_plex_dlna_stream_uri(uri)





def rating_key_from_embedded_uri(uri: str | None) -> str | None:

    """All URI-embedded ratingKey sources (query → PMS path → transcode object id)."""

    return rating_key_from_uri(uri)





def rating_key_from_dlna_cache(uri: str) -> str | None:

    """DLNA URL cache and ``object_id`` map (no URI-embedded ratingKey re-parse)."""

    from plex.url_resolver import get_url_resolver



    return get_url_resolver().rating_key_from_dlna_cache_only(uri)





async def rating_key_from_queue_object_scan(adapter, uri: str) -> str | None:

    """Match polled ``object_id`` against resolved playQueue stream URLs."""

    from plex.url_resolver import get_url_resolver



    object_id = object_id_from_stream_url(uri)

    if not object_id or adapter.queue is None:

        return None

    resolver = get_url_resolver()

    total = await adapter.queue.total_count()

    limit = int(total) if not math.isinf(total) else await adapter.queue.available_count()

    for offset in range(limit):

        try:

            track = await adapter.queue.track(offset)

            track_url = await resolver.resolve_stream_url(track)

            if object_id_from_stream_url(track_url) == object_id:

                await adapter.queue.set_selected_offset(offset)

                return str(track.ratingKey)

        except (LookupError, IndexError, ValueError):

            continue

    return None





async def rating_key_for_polled_uri(

    adapter,

    uri: str,

    *,

    skip_query_param: bool = False,

    skip_pms_uri: bool = False,

    skip_sonoplay_object: bool = False,

) -> str | None:

    """Map SM6 TrackURI → Plex ratingKey (most → least reliable).



    1. ``?ratingKey=`` query param (any tagger: SonoPlay, SM6, third party)

    2. PMS metadata URI (``server://…/library/metadata/{id}``, ``/library/metadata/{id}``)

    3. SonoPlay transcode object id (``sonoplay-tc-{id}``)

    4. DLNA stream URL / ``object_id`` cache

    5. playQueue scan by matching DLNA ``object_id``

    """

    if not skip_query_param:

        query_key = rating_key_from_query_param(uri)

        if query_key:

            return query_key



    if not skip_pms_uri:

        pms_key = rating_key_from_pms_uri(uri)

        if pms_key:

            return pms_key



    if not skip_sonoplay_object:

        transcode_key = rating_key_from_sonoplay_transcode_object(uri)

        if transcode_key:

            return transcode_key



    cached = rating_key_from_dlna_cache(uri)

    if cached:

        return cached



    return await rating_key_from_queue_object_scan(adapter, uri)


