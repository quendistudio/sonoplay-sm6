"""RenderingControl SetVolume/SetMute for SM6 (Cambridge SOAP headers)."""
from __future__ import annotations

import asyncio
import logging
from html import escape as xml_escape

from settings import settings
from utils import UPNP_RC_SERVICE_TYPE, g

logger = logging.getLogger(__name__)

_LOCKS: dict[str, asyncio.Lock] = {}
_PENDING_VOLUME: dict[str, tuple[object, int]] = {}
_VOLUME_WRITING: dict[str, bool] = {}
_VOLUME_DEBOUNCE_TASKS: dict[str, asyncio.Task] = {}

_PAYLOAD = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    '<s:Body>'
    '<u:{action} xmlns:u="{urn}">'
    '{fields}'
    '</u:{action}>'
    '</s:Body>'
    '</s:Envelope>'
)


def is_sm6_proxy_url(url: str | None) -> bool:
    """DLNA proxy URL (plexupnp :8050, etc.) — not the native renderer."""
    if not url:
        return False
    lowered = url.casefold()
    return ":8050/" in lowered or ":8050" in lowered


def sm6_device_priority(device) -> int:
    """Preference score: 0 = native SM6, higher = proxy / less reliable."""
    url = getattr(device, "location_url", None) or ""
    if is_sm6_proxy_url(url):
        return 10
    return 0


def pick_sm6_control_device(candidates: list) -> list:
    """One entry per UUID, preferring native URL (not :8050)."""
    if not candidates:
        return []
    best: dict[str, object] = {}
    for device in candidates:
        uuid = getattr(device, "uuid", None)
        if not uuid:
            continue
        existing = best.get(uuid)
        if existing is None or sm6_device_priority(device) < sm6_device_priority(existing):
            best[uuid] = device
    return list(best.values())


def sm6_preferred_description_url(device) -> str:
    """Native description.xml URL when available (avoids Plex :8050 proxy)."""
    uuid = getattr(device, "uuid", None)
    candidates = [device]
    if uuid:
        try:
            from dlna.dlna_device import devices as physical_devices
        except Exception:
            physical_devices = []
        for candidate in physical_devices:
            if getattr(candidate, "uuid", None) == uuid and candidate is not device:
                candidates.append(candidate)
    picked = pick_sm6_control_device(candidates)
    if picked:
        return str(getattr(picked[0], "location_url", "") or "")
    return str(getattr(device, "location_url", "") or "")


def _device_uuid(device) -> str:
    return getattr(device, "uuid", None) or str(id(device))


def _lock_for(device) -> asyncio.Lock:
    key = _device_uuid(device)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def sm6_volume_poll_paused(device) -> bool:
    """True while a volume write is scheduled or SOAP is in flight."""
    uuid = _device_uuid(device)
    return bool(_VOLUME_WRITING.get(uuid) or uuid in _PENDING_VOLUME)


def _volume_debounce_seconds() -> float:
    return getattr(settings, "sm6_volume_debounce_seconds", 0.15)


def _reschedule_volume_flush(uuid: str) -> None:
    task = _VOLUME_DEBOUNCE_TASKS.get(uuid)
    if task is not None and not task.done():
        task.cancel()
    _VOLUME_DEBOUNCE_TASKS[uuid] = asyncio.create_task(
        _volume_debounce_worker(uuid),
        name=f"sm6-volume-{uuid[:8]}",
    )


async def _volume_debounce_worker(uuid: str) -> None:
    try:
        await asyncio.sleep(_volume_debounce_seconds())
    except asyncio.CancelledError:
        return

    entry = _PENDING_VOLUME.pop(uuid, None)
    _VOLUME_DEBOUNCE_TASKS.pop(uuid, None)
    if entry is None:
        return

    device, clamped = entry
    await _apply_set_volume(device, clamped)


async def _apply_set_volume(device, clamped: int) -> bool:
    uuid = _device_uuid(device)
    _VOLUME_WRITING[uuid] = True
    try:
        url = await _rendering_control_url(device)
        logger.info(
            "%s SM6 SetVolume DesiredVolume=%s url=%s",
            device.name,
            clamped,
            url,
        )
        from dlna.sm6_dispatcher import get_sm6_dispatcher

        dispatcher = get_sm6_dispatcher(uuid, sm6_preferred_description_url(device))

        async def _send() -> bool:
            return await _sm6_rc_post(device, "SetVolume", InstanceID=0, Channel="Master", DesiredVolume=clamped)

        return await dispatcher.submit_control(_send, label=f"SetVolume {clamped}")
    finally:
        _VOLUME_WRITING[uuid] = False
        if uuid in _PENDING_VOLUME:
            _reschedule_volume_flush(uuid)


def _build_fields(**kwargs) -> str:
    parts = []
    for tag, value in kwargs.items():
        parts.append(f"<{tag}>{xml_escape(str(value))}</{tag}>")
    return "".join(parts)


async def _rendering_control_url(device) -> str | None:
    await device.get_data()
    service = device._get_service(UPNP_RC_SERVICE_TYPE)
    if service is None:
        return None
    url = str(service.control_url or "")
    preferred = sm6_preferred_description_url(device)
    if preferred and is_sm6_proxy_url(url):
        from urllib.parse import urlparse, urlunparse

        pref = urlparse(preferred)
        cur = urlparse(url)
        path = cur.path or ""
        marker = "/RenderingControl/"
        if marker in path:
            path = path[path.find(marker) :]
        url = urlunparse((pref.scheme or "http", pref.netloc, path, "", "", ""))
    return url or None


async def _sm6_rc_post(device, action: str, **fields) -> bool:
    """Post a RenderingControl action (caller holds dispatcher soap lock)."""
    url = await _rendering_control_url(device)
    if not url:
        logger.warning("%s SM6 %s: RenderingControl not found", device.name, action)
        return False

    urn = UPNP_RC_SERVICE_TYPE
    body = _PAYLOAD.format(action=action, urn=urn, fields=_build_fields(**fields))
    headers = {
        "SOAPAction": f'"{urn}#{action}"',
        'Content-Type': 'text/xml; charset="utf-8"',
    }

    try:
        async with g.http.post(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=settings.http_timeout_dlna,
        ) as response:
            text = await response.text()
            if response.status == 200:
                logger.debug(
                    "%s SM6 %s OK url=%s fields=%s",
                    device.name,
                    action,
                    url,
                    fields,
                )
                return True
            logger.warning(
                "%s SM6 %s HTTP %s url=%s fields=%s body=%s",
                device.name,
                action,
                response.status,
                url,
                fields,
                (text or "")[:800],
            )
            return False
    except Exception as exc:
        logger.warning("%s SM6 %s error url=%s: %s", device.name, action, url, exc)
        return False


async def sm6_rc_action(device, action: str, **fields) -> bool:
    """Post a RenderingControl action with Cambridge-style SOAP headers."""
    from dlna.sm6_dispatcher import get_sm6_dispatcher

    uuid = _device_uuid(device)
    dispatcher = get_sm6_dispatcher(uuid, sm6_preferred_description_url(device))

    async def _send() -> bool:
        return await _sm6_rc_post(device, action, **fields)

    return await dispatcher.submit_control(_send, label=action)


async def sm6_set_volume(device, desired: int) -> bool:
    """Schedule coalesced SetVolume (last value after debounce)."""
    await device.get_volume_info()
    vr = device.volume_range()
    clamped = max(vr.minimum, min(vr.maximum, int(desired)))
    uuid = _device_uuid(device)
    previous = _PENDING_VOLUME.get(uuid)
    _PENDING_VOLUME[uuid] = (device, clamped)
    if previous is None or previous[1] != clamped:
        logger.debug(
            "%s SM6 SetVolume coalesced DesiredVolume=%s (range %s..%s)",
            device.name,
            clamped,
            vr.minimum,
            vr.maximum,
        )
    _reschedule_volume_flush(uuid)
    return True


async def sm6_set_mute(device, muted: bool) -> bool:
    return await sm6_rc_action(
        device,
        "SetMute",
        InstanceID=0,
        Channel="Master",
        DesiredMute=1 if muted else 0,
    )
