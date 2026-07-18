"""Browse Plex DLNA ContentDirectory (Plex media server DLNA endpoint)."""
from __future__ import annotations

import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from utils import g

logger = logging.getLogger(__name__)

NS = {
    "d": "urn:schemas-upnp-org:device-1-0",
    "s": "http://schemas.xmlsoap.org/soap/envelope/",
    "u": "urn:schemas-upnp-org:service:ContentDirectory:1",
}

SOAP_HEADERS = {
    "Content-Type": 'text/xml; charset="utf-8"',
    "SOAPAction": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
}


@dataclass(frozen=True)
class DlnaItem:
    object_id: str
    title: str
    is_container: bool
    url: str | None = None
    mime: str | None = None


class DlnaBrowser:
    def __init__(self, device_url: str, *, timeout: float = 30.0) -> None:
        self._device_url = device_url
        self._timeout = timeout
        self._control_url: str | None = None

    async def _ensure_control_url(self) -> str:
        if self._control_url:
            return self._control_url
        async with g.http.get(self._device_url, timeout=self._timeout) as response:
            response.raise_for_status()
            desc = await response.text()
        base = self._device_url.rsplit("/", 1)[0]
        root = ET.fromstring(desc)
        for svc in root.findall(".//d:service", NS):
            st = svc.findtext("d:serviceType", default="", namespaces=NS)
            if st != "urn:schemas-upnp-org:service:ContentDirectory:1":
                continue
            ctrl = svc.findtext("d:controlURL", default="", namespaces=NS)
            self._control_url = ctrl if ctrl.startswith("http") else base + ctrl
            return self._control_url
        raise RuntimeError("ContentDirectory not found in DeviceDescription.xml")

    async def browse(self, object_id: str = "0", *, count: int = 200) -> list[DlnaItem]:
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <ObjectID>{object_id}</ObjectID>
      <BrowseFlag>BrowseDirectChildren</BrowseFlag>
      <Filter>*</Filter>
      <StartingIndex>0</StartingIndex>
      <RequestedCount>{count}</RequestedCount>
      <SortCriteria></SortCriteria>
    </u:Browse>
  </s:Body>
</s:Envelope>"""
        control = await self._ensure_control_url()
        async with g.http.post(control, data=body, headers=SOAP_HEADERS, timeout=self._timeout) as response:
            response.raise_for_status()
            text = await response.text()
        return _parse_browse_result(text)

    async def browse_object_didl(self, object_id: str) -> str:
        """DIDL-Lite for a Plex object (BrowseMetadata) for SM6 QueueFolder."""
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <ObjectID>{object_id}</ObjectID>
      <BrowseFlag>BrowseMetadata</BrowseFlag>
      <Filter>*</Filter>
      <StartingIndex>0</StartingIndex>
      <RequestedCount>1</RequestedCount>
      <SortCriteria></SortCriteria>
    </u:Browse>
  </s:Body>
</s:Envelope>"""
        control = await self._ensure_control_url()
        async with g.http.post(control, data=body, headers=SOAP_HEADERS, timeout=self._timeout) as response:
            response.raise_for_status()
            text = await response.text()
        return _extract_browse_result_didl(text)


def _extract_browse_result_didl(soap_xml: str) -> str:
    root = ET.fromstring(soap_xml)
    for element in root.iter():
        if element.tag.endswith("Result") and element.text:
            return html.unescape(element.text.strip())
    raise LookupError("BrowseMetadata response missing Result DIDL")


def _parse_browse_result(soap_xml: str) -> list[DlnaItem]:
    root = ET.fromstring(soap_xml)
    result_el = None
    for el in root.iter():
        if el.tag.endswith("Result"):
            result_el = el
            break
    if result_el is None or not result_el.text:
        return []
    didl = html.unescape(result_el.text)
    try:
        didl_root = ET.fromstring(didl)
        return _parse_didl_element_tree(didl_root)
    except ET.ParseError:
        return _parse_didl_regex(didl)


def _parse_didl_element_tree(didl_root: ET.Element) -> list[DlnaItem]:
    items: list[DlnaItem] = []
    for node in list(didl_root):
        tag = node.tag.rsplit("}", 1)[-1]
        object_id = node.attrib.get("id", "")
        restricted = node.attrib.get("restricted", "0")
        title = _child_text(node, "title") or object_id
        upnp_class = _child_text(node, "class") or ""
        is_container = tag == "container" or "container" in upnp_class
        url = None
        mime = None
        for child in node:
            if child.tag.rsplit("}", 1)[-1] == "res" and child.text:
                url = child.text.strip()
                protocol = child.attrib.get("protocolInfo", "")
                mime = protocol.split(":")[2] if ":" in protocol else None
                break
        if is_container and restricted == "0":
            is_container = True
        items.append(DlnaItem(object_id=object_id, title=title, is_container=is_container, url=url, mime=mime))
    return items


def _parse_didl_regex(didl: str) -> list[DlnaItem]:
    items: list[DlnaItem] = []
    for match in re.finditer(r"<(container|item)\b([^>]*)>(.*?)</\1>", didl, re.S):
        tag, block_attrs, block = match.group(1), match.group(2), match.group(3)
        block = block_attrs + ">" + block
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', match.group(2)))
        object_id = attrs.get("id", "")
        title_m = re.search(r"<dc:title>(.*?)</dc:title>", block, re.S)
        class_m = re.search(r"<upnp:class>(.*?)</upnp:class>", block, re.S)
        res_m = re.search(r"<res[^>]*>(.*?)</res>", block, re.S)
        title = html.unescape(title_m.group(1).strip()) if title_m else object_id
        upnp_class = class_m.group(1) if class_m else ""
        is_container = tag == "container" or "container" in upnp_class
        url = html.unescape(res_m.group(1).strip()) if res_m else None
        items.append(DlnaItem(object_id=object_id, title=title, is_container=is_container, url=url))
    return items


def _child_text(node: ET.Element, local: str) -> str | None:
    for child in node:
        if child.tag.rsplit("}", 1)[-1] == local:
            return (child.text or "").strip() or None
    return None


def _album_part_matches(album_part: str, album_title: str) -> bool:
    part = album_part.casefold().strip()
    target = album_title.casefold().strip()
    if part == target:
        return True
    return part.startswith(target + " (") or part.startswith(target + "(")


def album_title_matches(dlna_title: str, album_title: str, *, artist: str | None = None) -> bool:
    name = dlna_title.casefold().strip()
    target = album_title.casefold().strip()
    if name == target:
        return True
    if _album_part_matches(name, album_title):
        return True
    if " - " in name:
        _, album_part = name.split(" - ", 1)
        if _album_part_matches(album_part, album_title):
            if artist is None or name.startswith(artist.casefold() + " - "):
                return True
    return False


async def child_by_title(browser: DlnaBrowser, object_id: str, title: str, *, contains: bool = False) -> DlnaItem:
    target = title.casefold()
    for item in await browser.browse(object_id, count=2000):
        name = item.title.casefold()
        if name == target or (contains and target in name):
            return item
    raise LookupError(f'"{title}" not found under {object_id}')


_BY_ALBUM_SECTION_TITLES = ("By Album", "Par album", "Par Album")
_BY_ARTIST_SECTION_TITLES = ("By Artist", "Par Artiste", "Par artiste")


async def find_browse_section(
    browser: DlnaBrowser,
    musique_id: str,
    *title_aliases: str,
) -> DlnaItem:
    """Find a Plex DLNA browse section (By Album, By Artist, …)."""
    aliases = {title.casefold() for title in title_aliases}
    for item in await browser.browse(musique_id, count=200):
        if item.title.casefold() in aliases:
            return item
    raise LookupError(f"Section {title_aliases!r} not found under {musique_id}")


def _artist_folder_matches(folder_title: str, artist: str) -> bool:
    name = folder_title.casefold().strip()
    target = artist.casefold().strip()
    if name == target:
        return True
    return name.startswith(target + " - ") or name.startswith(target + " (")


async def _find_artist_folder(browser: DlnaBrowser, by_artist_id: str, artist: str) -> DlnaItem:
    for item in await browser.browse(by_artist_id, count=500):
        if item.is_container and _artist_folder_matches(item.title, artist):
            return item
    raise LookupError(f'Artist "{artist}" not found in Plex DLNA')


async def _find_album_via_artist(
    browser: DlnaBrowser,
    musique_id: str,
    album_title: str,
    *,
    artist: str,
) -> DlnaItem:
    by_artist = await find_browse_section(browser, musique_id, *_BY_ARTIST_SECTION_TITLES)
    artist_item = await _find_artist_folder(browser, by_artist.object_id, artist)
    candidates = [
        item
        for item in await browser.browse(artist_item.object_id, count=300)
        if item.is_container and album_title_matches(item.title, album_title, artist=artist)
    ]
    if not candidates:
        raise LookupError(f'Album "{album_title}" not found under artist "{artist}"')
    if len(candidates) == 1:
        return candidates[0]
    return min(candidates, key=lambda item: len(item.title))


async def find_album(
    browser: DlnaBrowser,
    musique_id: str,
    album_title: str,
    *,
    artist: str | None = None,
) -> DlnaItem:
    if artist:
        try:
            item = await _find_album_via_artist(
                browser,
                musique_id,
                album_title,
                artist=artist,
            )
            logger.info(
                'Plex DLNA album via artist path: %r (artist=%r)',
                item.title,
                artist,
            )
            return item
        except LookupError as exc:
            logger.debug(
                "Plex DLNA artist browse path failed for %r / %r: %s",
                artist,
                album_title,
                exc,
            )

    by_album = await find_browse_section(browser, musique_id, *_BY_ALBUM_SECTION_TITLES)
    candidates = [
        item
        for item in await browser.browse(by_album.object_id, count=2000)
        if album_title_matches(item.title, album_title, artist=artist)
    ]
    if not candidates:
        raise LookupError(f'Album "{album_title}" not found in Plex DLNA')
    if artist:
        for item in candidates:
            if item.title.casefold().startswith(artist.casefold() + " - "):
                return item
    if len(candidates) == 1:
        return candidates[0]
    return min(candidates, key=lambda item: len(item.title))


async def browse_album_tracks(
    browser: DlnaBrowser,
    musique_id: str,
    album_title: str,
    *,
    artist: str | None = None,
) -> list[DlnaItem]:
    album = await find_album(browser, musique_id, album_title, artist=artist)
    return [i for i in await browser.browse(album.object_id, count=200) if not i.is_container and i.url]


async def find_track_in_album(
    browser: DlnaBrowser,
    musique_id: str,
    album_title: str,
    track_title: str,
    *,
    artist: str | None = None,
) -> DlnaItem:
    album = await find_album(browser, musique_id, album_title, artist=artist)
    target = track_title.casefold()
    for item in await browser.browse(album.object_id, count=200):
        if not item.is_container and item.title.casefold() == target:
            return item
    raise LookupError(f'"{track_title}" not found in "{album_title}"')


async def plex_dlna_server_udn(device_url: str, *, timeout: float = 30.0) -> str:
    async with g.http.get(device_url, timeout=timeout) as response:
        response.raise_for_status()
        desc = await response.text()
    root = ET.fromstring(desc)
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "UDN" and element.text:
            return element.text.strip().removeprefix("uuid:")
    raise RuntimeError("Plex DLNA UDN not found in DeviceDescription.xml")


def machine_identifier_to_dlna_udn(machine_identifier: str) -> str:
    """Derive Plex DLNA ServerUDN from PMS machineIdentifier (first 128 bits as UUID)."""
    hex_id = str(machine_identifier or "").replace("-", "").lower()
    if len(hex_id) < 32:
        raise ValueError(f"machineIdentifier too short for DLNA UDN: {machine_identifier!r}")
    prefix = hex_id[:32]
    return (
        f"{prefix[0:8]}-{prefix[8:12]}-{prefix[12:16]}"
        f"-{prefix[16:20]}-{prefix[20:32]}"
    )


async def resolve_plex_dlna_server_udn(
    device_url: str | None = None,
    *,
    machine_identifier: str | None = None,
    timeout: float = 8.0,
) -> str:
    """Resolve Plex DLNA ServerUDN: cache → DeviceDescription → machineIdentifier."""
    from plex.runtime_cache import (
        cached_plex_dlna_server_udn,
        cached_plex_session,
        remember_plex_dlna_server_udn,
    )

    cached = cached_plex_dlna_server_udn()
    if cached:
        return cached

    if device_url:
        try:
            udn = await plex_dlna_server_udn(device_url, timeout=timeout)
            remember_plex_dlna_server_udn(udn)
            return udn
        except Exception as exc:
            logger.warning(
                "Plex DLNA DeviceDescription unreachable (%s); trying machineIdentifier UDN",
                exc,
            )

    machine_id = machine_identifier or cached_plex_session().get("machine_id")
    if machine_id:
        udn = machine_identifier_to_dlna_udn(str(machine_id))
        remember_plex_dlna_server_udn(udn)
        logger.info("Plex DLNA ServerUDN derived from machineIdentifier: %s", udn)
        return udn

    raise RuntimeError(
        "Plex DLNA ServerUDN unavailable (port 32469 unreachable and no machineIdentifier)",
    )


async def plex_dlna_friendly_name(device_url: str, *, timeout: float = 30.0) -> str | None:
    async with g.http.get(device_url, timeout=timeout) as response:
        response.raise_for_status()
        desc = await response.text()
    root = ET.fromstring(desc)
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "friendlyName" and element.text:
            name = element.text.strip()
            return name or None
    return None


async def find_items_by_title(
    browser: DlnaBrowser,
    titles: set[str],
    *,
    root_id: str = "0",
    max_depth: int = 6,
) -> dict[str, DlnaItem]:
    wanted = {t.casefold() for t in titles}
    found: dict[str, DlnaItem] = {}

    async def walk(object_id: str, depth: int) -> None:
        if depth > max_depth or len(found) == len(wanted):
            return
        for item in await browser.browse(object_id):
            key = item.title.casefold()
            if not item.is_container and item.url and key in wanted and key not in found:
                found[key] = item
            if item.is_container:
                await walk(item.object_id, depth + 1)

    await walk(root_id, 0)
    return found


_BY_ALBUM_CHILD_TITLES = frozenset({"by album", "par album"})
_MUSIC_FOLDER_CHILD_TITLES = frozenset({"musique", "music"})


async def discover_plex_dlna_music_ids(
    browser: DlnaBrowser,
    *,
    max_depth: int = 5,
) -> dict[str, str | None]:
    """Find Plex DLNA object IDs for album browse and music folder roots."""
    musique_id: str | None = None
    music_folder_id: str | None = None

    async def scan(object_id: str, depth: int) -> None:
        nonlocal musique_id, music_folder_id
        if depth > max_depth or (musique_id and music_folder_id):
            return
        try:
            children = await browser.browse(object_id, count=200)
        except Exception as exc:
            logger.debug("Plex DLNA browse %s failed: %s", object_id, exc)
            return
        child_names = {item.title.casefold() for item in children if item.is_container}
        if musique_id is None and child_names & _BY_ALBUM_CHILD_TITLES:
            musique_id = object_id
        if music_folder_id is None and child_names & _MUSIC_FOLDER_CHILD_TITLES:
            music_folder_id = object_id
        for item in children:
            if item.is_container:
                await scan(item.object_id, depth + 1)

    await scan("0", 0)
    return {"musique_id": musique_id, "music_folder_id": music_folder_id}


def dlna_discovery_cache_path() -> Path:
    from settings import settings

    return Path(settings.config_path) / "plex_dlna_discovery.json"


def load_dlna_discovery_cache(device_url: str) -> dict[str, str | None] | None:
    path = dlna_discovery_cache_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load Plex DLNA discovery cache %s: %s", path, exc)
        return None
    if not isinstance(raw, dict) or raw.get("device_url") != device_url:
        return None
    musique_id = raw.get("musique_id")
    if not musique_id:
        return None
    return {
        "musique_id": str(musique_id),
        "music_folder_id": raw.get("music_folder_id"),
    }


def save_dlna_discovery_cache(device_url: str, ids: dict[str, str | None]) -> None:
    path = dlna_discovery_cache_path()
    payload = {
        "device_url": device_url,
        "musique_id": ids.get("musique_id"),
        "music_folder_id": ids.get("music_folder_id"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not persist Plex DLNA discovery cache: %s", exc)
