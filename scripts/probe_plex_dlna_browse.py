#!/usr/bin/env python3
"""Probe Plex DLNA ContentDirectory browse (diagnostics before code changes).

Standalone SOAP client — no SonoPlay app import.

Examples:
  python scripts/probe_plex_dlna_browse.py --host <pms-host>
  python scripts/probe_plex_dlna_browse.py --device-url http://<pms-host>:32469/DeviceDescription.xml
  python scripts/probe_plex_dlna_browse.py --host <pms-host> --musique-id <id> --object-id <track-id>
  python scripts/probe_plex_dlna_browse.py --host <pms-host> --album Third --artist Portishead
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOAP_HEADERS = {
    "Content-Type": 'text/xml; charset="utf-8"',
    "SOAPAction": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
}

NS = {"d": "urn:schemas-upnp-org:device-1-0"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Plex DLNA ContentDirectory browse.")
    parser.add_argument("--host", help="PMS host (builds :32469 DeviceDescription URL)")
    parser.add_argument("--port", type=int, default=32469, help="Plex DLNA port (default 32469)")
    parser.add_argument("--device-url", help="Full DeviceDescription.xml URL")
    parser.add_argument("--musique-id", help="Cached musique_id to test")
    parser.add_argument("--music-folder-id", help="Cached music_folder_id to test")
    parser.add_argument(
        "--object-id",
        action="append",
        default=[],
        help="Extra object ids for BrowseDirectChildren / BrowseMetadata (repeatable)",
    )
    parser.add_argument("--album", help="Album title to search (e.g. Third)")
    parser.add_argument("--artist", help="Artist for album search (e.g. Portishead)")
    parser.add_argument(
        "--counts",
        default="1,50,200",
        help="Comma-separated RequestedCount values for musique_id browse (default 1,50,200)",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--find-timeout",
        type=float,
        default=60.0,
        help="Max seconds for find_album probe (default 60)",
    )
    parser.add_argument(
        "--discovery-cache",
        type=Path,
        help="Path to plex_dlna_discovery.json (default: config/plex_dlna_discovery.json)",
    )
    return parser.parse_args()


def _device_url(args: argparse.Namespace) -> str:
    if args.device_url:
        return args.device_url.rstrip("/")
    if not args.host:
        raise SystemExit("Provide --host or --device-url")
    return f"http://{args.host}:{args.port}/DeviceDescription.xml"


def _load_discovery_cache(path: Path, device_url: str) -> dict[str, str | None]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARN  discovery cache unreadable: {exc}")
        return {}
    if raw.get("device_url") != device_url:
        print("WARN  discovery cache device_url mismatch — ignoring cached ids")
        return {}
    return {
        "musique_id": raw.get("musique_id"),
        "music_folder_id": raw.get("music_folder_id"),
    }


def _soap_body(object_id: str, *, browse_flag: str, count: int) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <ObjectID>{object_id}</ObjectID>
      <BrowseFlag>{browse_flag}</BrowseFlag>
      <Filter>*</Filter>
      <StartingIndex>0</StartingIndex>
      <RequestedCount>{count}</RequestedCount>
      <SortCriteria></SortCriteria>
    </u:Browse>
  </s:Body>
</s:Envelope>"""


def _parse_children(soap_xml: str) -> list[tuple[str, str, bool]]:
    root = ET.fromstring(soap_xml)
    result_el = None
    for el in root.iter():
        if el.tag.endswith("Result"):
            result_el = el
            break
    if result_el is None or not result_el.text:
        return []
    didl = html.unescape(result_el.text)
    items: list[tuple[str, str, bool]] = []
    try:
        didl_root = ET.fromstring(didl)
        for node in list(didl_root):
            tag = node.tag.rsplit("}", 1)[-1]
            object_id = node.attrib.get("id", "")
            title = ""
            for child in node:
                if child.tag.rsplit("}", 1)[-1] == "title":
                    title = (child.text or "").strip()
                    break
            upnp_class = ""
            for child in node:
                if child.tag.rsplit("}", 1)[-1] == "class":
                    upnp_class = (child.text or "").strip()
                    break
            is_container = tag == "container" or "container" in upnp_class
            items.append((object_id, title or object_id, is_container))
    except ET.ParseError:
        pass
    return items


async def _resolve_control_url(session: aiohttp.ClientSession, device_url: str, timeout: float) -> str:
    async with session.get(device_url, timeout=timeout) as response:
        text = await response.text()
        print(f"GET  {device_url} -> HTTP {response.status}")
        response.raise_for_status()
    base = device_url.rsplit("/", 1)[0]
    root = ET.fromstring(text)
    for svc in root.findall(".//d:service", NS):
        st = svc.findtext("d:serviceType", default="", namespaces=NS)
        if st != "urn:schemas-upnp-org:service:ContentDirectory:1":
            continue
        ctrl = svc.findtext("d:controlURL", default="", namespaces=NS)
        control = ctrl if ctrl.startswith("http") else urljoin(base + "/", ctrl.lstrip("/"))
        print(f"OK   ContentDirectory control: {control}")
        return control
    raise RuntimeError("ContentDirectory service not found")


def _upnp_fault_detail(soap_xml: str) -> str | None:
    try:
        root = ET.fromstring(soap_xml)
    except ET.ParseError:
        return None
    fault = None
    for el in root.iter():
        if el.tag.endswith("Fault"):
            fault = el
            break
    if fault is None:
        return None
    parts: list[str] = []
    for el in fault.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("faultcode", "faultstring", "errorCode", "errorDescription") and el.text:
            parts.append(f"{tag}={el.text.strip()}")
    return "; ".join(parts) if parts else None


async def _browse(
    session: aiohttp.ClientSession,
    control: str,
    object_id: str,
    *,
    browse_flag: str,
    count: int,
    timeout: float,
) -> tuple[int, str]:
    body = _soap_body(object_id, browse_flag=browse_flag, count=count)
    async with session.post(control, data=body, headers=SOAP_HEADERS, timeout=timeout) as response:
        text = await response.text()
        return response.status, text


def _print_children(label: str, children: list[tuple[str, str, bool]], limit: int = 25) -> None:
    print(f"      {label}: {len(children)} item(s)")
    for object_id, title, is_container in children[:limit]:
        kind = "container" if is_container else "item"
        print(f"        - [{kind}] {title!r} id={object_id}")
    if len(children) > limit:
        print(f"        ... ({len(children) - limit} more)")


async def _probe_browse(
    session: aiohttp.ClientSession,
    control: str,
    object_id: str,
    *,
    browse_flag: str,
    count: int,
    timeout: float,
) -> bool:
    flag_label = "children" if browse_flag == "BrowseDirectChildren" else "metadata"
    print(f"\nBROWSE {flag_label} object_id={object_id} count={count}")
    status, text = await _browse(
        session,
        control,
        object_id,
        browse_flag=browse_flag,
        count=count,
        timeout=timeout,
    )
    if status != 200:
        fault = _upnp_fault_detail(text)
        snippet = text.replace("\n", " ")[:240]
        print(f"FAIL HTTP {status}" + (f" ({fault})" if fault else ""))
        print(f"      body[:240]={snippet!r}")
        return False
    if browse_flag == "BrowseMetadata":
        try:
            root = ET.fromstring(text)
            for element in root.iter():
                if element.tag.endswith("Result") and element.text:
                    didl = html.unescape(element.text.strip())
                    print(f"OK   BrowseMetadata DIDL len={len(didl)}")
                    print(f"      {didl[:300]}{'...' if len(didl) > 300 else ''}")
                    return True
        except ET.ParseError as exc:
            print(f"FAIL parse BrowseMetadata: {exc}")
            return False
    children = _parse_children(text)
    _print_children("children", children)
    return True


def _device_url_from_control(control: str) -> str:
    return control.split("/ContentDirectory/", 1)[0] + "/DeviceDescription.xml"


async def _discover_ids(control: str, timeout: float) -> dict[str, str | None]:
    from plex.dlna_browser import DlnaBrowser, discover_plex_dlna_music_ids

    browser = DlnaBrowser(_device_url_from_control(control), timeout=timeout)
    ids = await discover_plex_dlna_music_ids(browser)
    print(f"OK   discover_plex_dlna_music_ids -> {ids}")
    return ids


async def _section_probe(control: str, musique_id: str, timeout: float) -> None:
    from plex.dlna_browser import (
        DlnaBrowser,
        _BY_ALBUM_SECTION_TITLES,
        _BY_ARTIST_SECTION_TITLES,
    )

    browser = DlnaBrowser(_device_url_from_control(control), timeout=timeout)
    children = await browser.browse(musique_id, count=50)
    titles = [item.title for item in children if item.is_container]
    print(f"\nSECTIONS under musique_id ({len(titles)} containers):")
    for title in titles:
        print(f"  - {title!r}")
    artist_aliases = {name.casefold() for name in _BY_ARTIST_SECTION_TITLES}
    album_aliases = {name.casefold() for name in _BY_ALBUM_SECTION_TITLES}
    print(
        "  artist aliases matched:",
        [t for t in titles if t.casefold() in artist_aliases] or "NONE",
    )
    print(
        "  album aliases matched:",
        [t for t in titles if t.casefold() in album_aliases] or "NONE",
    )
    if not any(t.casefold() == "all artists" for t in titles):
        print("  NOTE Plex shows 'All Artists' — not in _BY_ARTIST_SECTION_TITLES")


async def _find_album_probe(
    session: aiohttp.ClientSession,
    control: str,
    musique_id: str,
    album: str,
    artist: str | None,
    timeout: float,
) -> None:
    from plex.dlna_browser import DlnaBrowser, find_album

    browser = DlnaBrowser(_device_url_from_control(control), timeout=timeout)
    print(f"\nFIND_ALBUM musique_id={musique_id} album={album!r} artist={artist!r}")
    try:
        item = await asyncio.wait_for(
            find_album(
                browser,
                musique_id,
                album,
                artist=artist,
            ),
            timeout=timeout,
        )
        print(f"OK   found {item.title!r} id={item.object_id}")
        await _probe_browse(
            session,
            control,
            item.object_id,
            browse_flag="BrowseMetadata",
            count=1,
            timeout=timeout,
        )
    except TimeoutError:
        print(f"FAIL find_album: timed out after {timeout}s (DLNA 500 retries?)")
    except Exception as exc:
        print(f"FAIL find_album: {type(exc).__name__}: {exc}")


async def main() -> int:
    args = _parse_args()
    device_url = _device_url(args)
    cache_path = args.discovery_cache or (ROOT / "config" / "plex_dlna_discovery.json")
    cached = _load_discovery_cache(cache_path, device_url)
    musique_id = args.musique_id or cached.get("musique_id")
    music_folder_id = args.music_folder_id or cached.get("music_folder_id")

    counts = []
    for part in args.counts.split(","):
        part = part.strip()
        if part:
            counts.append(int(part))

    print(f"Probe Plex DLNA browse")
    print(f"  device_url={device_url}")
    if cache_path.exists():
        print(f"  discovery_cache={cache_path}")
    if musique_id:
        print(f"  musique_id={musique_id}")
    if music_folder_id:
        print(f"  music_folder_id={music_folder_id}")

    timeout = aiohttp.ClientTimeout(total=args.timeout)
    from utils import g

    async with aiohttp.ClientSession(timeout=timeout) as session:
        g.http = session
        try:
            control = await _resolve_control_url(session, device_url, args.timeout)
        except Exception as exc:
            print(f"FAIL device description: {exc}")
            return 1

        ok = await _probe_browse(
            session,
            control,
            "0",
            browse_flag="BrowseDirectChildren",
            count=50,
            timeout=args.timeout,
        )
        if not ok:
            print("\nSTOP root browse failed — fix DLNA / network before album lookup.")
            return 2

        if not musique_id or not music_folder_id:
            try:
                discovered = await _discover_ids(control, args.timeout)
                musique_id = musique_id or discovered.get("musique_id")
                music_folder_id = music_folder_id or discovered.get("music_folder_id")
            except Exception as exc:
                print(f"WARN discover_plex_dlna_music_ids: {exc}")

        if musique_id:
            try:
                await _section_probe(control, str(musique_id), args.timeout)
            except Exception as exc:
                print(f"WARN section probe: {exc}")
            for count in counts:
                ok = await _probe_browse(
                    session,
                    control,
                    str(musique_id),
                    browse_flag="BrowseDirectChildren",
                    count=count,
                    timeout=args.timeout,
                )
                if not ok:
                    print(f"NOTE musique_id browse fails at count={count}")

        if music_folder_id:
            await _probe_browse(
                session,
                control,
                str(music_folder_id),
                browse_flag="BrowseDirectChildren",
                count=50,
                timeout=args.timeout,
            )

        for object_id in args.object_id:
            await _probe_browse(
                session,
                control,
                object_id,
                browse_flag="BrowseDirectChildren",
                count=10,
                timeout=args.timeout,
            )
            await _probe_browse(
                session,
                control,
                object_id,
                browse_flag="BrowseMetadata",
                count=1,
                timeout=args.timeout,
            )

        if args.album and musique_id:
            await _find_album_probe(
                session,
                control,
                str(musique_id),
                args.album,
                args.artist,
                args.find_timeout,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
