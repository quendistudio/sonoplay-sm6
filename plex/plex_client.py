"""Plex client identification and volume settings (Plexamp)."""
from __future__ import annotations

import logging

from settings import settings

logger = logging.getLogger(__name__)

_client_products: dict[str, str] = {}


def note_client_product(client_uuid: str | None, product: str | None) -> None:
    """Remember the Plex product associated with a controller client."""
    if not client_uuid or not product:
        return
    _client_products[client_uuid] = product.strip()


def client_product(client_uuid: str | None) -> str | None:
    if not client_uuid:
        return None
    return _client_products.get(client_uuid)


def is_plexamp_client(
    *,
    client_uuid: str | None = None,
    product: str | None = None,
) -> bool:
    """True if the request comes from Plexamp (not Plex for Windows, etc.)."""
    candidates = [product, client_product(client_uuid)]
    for value in candidates:
        if value and "plexamp" in value.casefold():
            return True
    return False


def resolve_sm6_volume_for_client(
    adapter,
    requested_percent: int,
    *,
    client_uuid: str | None = None,
    product: str | None = None,
    device_step: int | None = None,
) -> tuple[int, int | None]:
    """Return (Plex volume, optional SM6 volume step) for set_volume."""
    note_client_product(client_uuid, product)
    if not settings.sm6_plexamp_volume_step_enabled:
        return requested_percent, None
    if not adapter._is_sm6_renderer():
        return requested_percent, None

    from dlna.sm6_volume import (
        plex_for_step,
        plexamp_volume_step,
    )

    current = adapter.state.volume
    if current is None:
        current = requested_percent
    delta = int(requested_percent) - int(current)
    max_delta = int(settings.sm6_plexamp_volume_step_max_delta)
    if abs(delta) > max_delta or delta == 0:
        return requested_percent, None
    if not is_plexamp_client(client_uuid=client_uuid, product=product):
        return requested_percent, None

    step = plexamp_volume_step(
        int(current),
        int(requested_percent),
        hardware_step=device_step,
        max_step_delta=max_delta,
    )
    if step is None:
        return requested_percent, None
    plex_volume = plex_for_step(step)
    if plex_volume != requested_percent:
        logger.info(
            "SM6 volume step (Plexamp) %s%% -> step %s (%s%% Plex, client=%s)",
            requested_percent,
            step,
            plex_volume,
            client_uuid or product or "?",
        )
    return plex_volume, step
