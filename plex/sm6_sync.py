"""SM6 passive sync helpers (ratingKey lookup from polled URIs)."""
from __future__ import annotations

import math

from plex.dlna_stream_cache import (
    is_plex_dlna_stream_uri,
    object_id_from_stream_url,
    rating_key_from_uri,
)


def is_plex_resolvable_uri(uri: str | None) -> bool:
    """True when TrackURI can map to Plex content (embedded key, DLNA object, or cache)."""
    return is_plex_dlna_stream_uri(uri)


async def rating_key_for_polled_uri(adapter, uri: str) -> str | None:
    """Map SM6 TrackURI → Plex ratingKey (URI → cache → queue scan)."""
    from plex.url_resolver import get_url_resolver

    embedded = rating_key_from_uri(uri)
    if embedded:
        return embedded

    resolver = get_url_resolver()
    rating_key = resolver.rating_key_for_stream_url(uri)
    if rating_key or not adapter.queue:
        return rating_key
    object_id = object_id_from_stream_url(uri)
    if not object_id:
        return None
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
