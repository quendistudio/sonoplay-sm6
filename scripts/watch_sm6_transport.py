#!/usr/bin/env python3
"""Poll SM6 AVTransport + playlist index until Ctrl+C (auto-next diagnostics).

Standalone — no SonoPlay app import (avoids dlna/plex circular import).

Use the **native** SM6 description.xml URL (not Plex :8050 proxy).

Example:
  $env:SM6_TEST_DESCRIPTION_URL = "http://192.168.50.42/description.xml"
  python scripts/watch_sm6_transport.py

  python scripts/watch_sm6_transport.py --description-url http://192.168.50.42/description.xml --interval 0.25
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime
from urllib.parse import urljoin

import aiohttp

AVT_NS = "urn:schemas-upnp-org:service:AVTransport:1"
UUVOL_NS = "urn:UuVol-com:service:UuVolControl:5"

_AVT_CONTROL = re.compile(
    r"<serviceType>\s*urn:schemas-upnp-org:service:AVTransport:1\s*</serviceType>"
    r".*?<controlURL>\s*([^<]+?)\s*</controlURL>",
    re.I | re.S,
)
_FRIENDLY_NAME = re.compile(r"<friendlyName>\s*([^<]+?)\s*</friendlyName>", re.I)
_TRANSPORT_STATE = re.compile(r"<CurrentTransportState>\s*([^<]+?)\s*</CurrentTransportState>", re.I)
_REL_TIME = re.compile(r"<RelTime>\s*([^<]+?)\s*</RelTime>", re.I)
_TRACK_URI = re.compile(r"<TrackURI>\s*([^<]*?)\s*</TrackURI>", re.I)
_MEDIA_QUEUE_INDEX = re.compile(r"<MediaQueueIndex>\s*(-?\d+)\s*</MediaQueueIndex>", re.I)
_CURRENT_TRACK_ID = re.compile(r"<CurrentPlaylistTrackID>\s*(\d+)\s*</CurrentPlaylistTrackID>", re.I)

_SOAP = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>{body}</s:Body></s:Envelope>"
)


def is_sm6_proxy_url(url: str) -> bool:
    lowered = url.casefold()
    return ":8050/" in lowered or lowered.endswith(":8050")


def reciva_radio_invoke_url(description_url: str) -> str:
    base = description_url.rsplit("/", 1)[0]
    return f"{base}/RecivaRadio/invoke"


def _uri_hint(uri: str | None) -> str:
    if not uri:
        return "-"
    if "ratingKey=" in uri:
        return "ratingKey=" + uri.split("ratingKey=", 1)[1].split("&", 1)[0]
    if len(uri) > 72:
        return uri[:40] + "…" + uri[-24:]
    return uri


async def _soap_post(
    session: aiohttp.ClientSession,
    url: str,
    *,
    body: str,
    soap_action: str,
) -> str:
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": soap_action,
    }
    async with session.post(url, data=body, headers=headers) as response:
        text = await response.text()
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: {text[:200]}")
        return text


async def _load_endpoints(
    session: aiohttp.ClientSession,
    description_url: str,
) -> tuple[str, str, str]:
    async with session.get(description_url) as response:
        response.raise_for_status()
        xml = await response.text()
    avt_match = _AVT_CONTROL.search(xml)
    if not avt_match:
        raise RuntimeError("AVTransport controlURL not found in description.xml")
    avt_control = urljoin(description_url, avt_match.group(1).strip())
    name_match = _FRIENDLY_NAME.search(xml)
    friendly = name_match.group(1).strip() if name_match else "?"
    return friendly, avt_control, reciva_radio_invoke_url(description_url)


class Sm6TransportWatcher:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        description_url: str,
        avt_control_url: str,
        reciva_url: str,
        friendly_name: str,
    ) -> None:
        self._session = session
        self._description_url = description_url
        self._avt_control = avt_control_url
        self._reciva_url = reciva_url
        self.friendly_name = friendly_name

    async def get_transport_state(self) -> str:
        body = _SOAP.format(
            body=(
                f'<u:GetTransportInfo xmlns:u="{AVT_NS}">'
                "<InstanceID>0</InstanceID>"
                "</u:GetTransportInfo>"
            )
        )
        xml = await _soap_post(
            self._session,
            self._avt_control,
            body=body,
            soap_action=f'"{AVT_NS}#GetTransportInfo"',
        )
        match = _TRANSPORT_STATE.search(xml)
        return match.group(1).strip() if match else "?"

    async def get_position(self) -> tuple[str, str]:
        body = _SOAP.format(
            body=(
                f'<u:GetPositionInfo xmlns:u="{AVT_NS}">'
                "<InstanceID>0</InstanceID>"
                "</u:GetPositionInfo>"
            )
        )
        xml = await _soap_post(
            self._session,
            self._avt_control,
            body=body,
            soap_action=f'"{AVT_NS}#GetPositionInfo"',
        )
        rel = _REL_TIME.search(xml)
        uri = _TRACK_URI.search(xml)
        return (
            rel.group(1).strip() if rel else "-",
            _uri_hint(uri.group(1).strip() if uri else ""),
        )

    async def get_queue_position(self) -> tuple[int, int]:
        index_body = _SOAP.format(
            body=f'<u:GetMediaQueueIndex xmlns:u="{UUVOL_NS}"/>',
        )
        track_body = _SOAP.format(
            body=f'<u:GetCurrentPlaylistTrack xmlns:u="{UUVOL_NS}"/>',
        )
        index_xml, track_xml = await asyncio.gather(
            _soap_post(
                self._session,
                self._reciva_url,
                body=index_body,
                soap_action=f'"{UUVOL_NS}#GetMediaQueueIndex"',
            ),
            _soap_post(
                self._session,
                self._reciva_url,
                body=track_body,
                soap_action=f'"{UUVOL_NS}#GetCurrentPlaylistTrack"',
            ),
        )
        index_match = _MEDIA_QUEUE_INDEX.search(index_xml)
        track_match = _CURRENT_TRACK_ID.search(track_xml)
        return (
            int(track_match.group(1)) if track_match else 0,
            int(index_match.group(1)) if index_match else -1,
        )

    async def sample(self) -> dict:
        row: dict = {}
        try:
            row["transport"] = await self.get_transport_state()
        except Exception as exc:
            row["transport"] = f"ERR:{exc.__class__.__name__}"
        try:
            rel, uri = await self.get_position()
            row["rel"] = rel
            row["uri"] = uri
        except Exception as exc:
            row["rel"] = "-"
            row["uri"] = f"ERR:{exc.__class__.__name__}"
        try:
            track_id, queue_index = await self.get_queue_position()
            row["track_id"] = track_id
            row["queue_index"] = queue_index
        except Exception as exc:
            row["track_id"] = "?"
            row["queue_index"] = "?"
            row["queue_err"] = exc.__class__.__name__
        return row


def _format_row(row: dict) -> str:
    parts = [
        f"transport={row.get('transport', '?')}",
        f"idx={row.get('queue_index', '?')}",
        f"track_id={row.get('track_id', '?')}",
        f"rel={row.get('rel', '-')}",
        f"uri={row.get('uri', '-')}",
    ]
    if "queue_err" in row:
        parts.append(f"queue_err={row['queue_err']}")
    return "  ".join(parts)


async def _run(args: argparse.Namespace) -> int:
    url = os.environ.get("SM6_TEST_DESCRIPTION_URL") or args.description_url
    if not url:
        print("Set SM6_TEST_DESCRIPTION_URL or pass --description-url", file=sys.stderr)
        return 2
    if is_sm6_proxy_url(url):
        print(
            "WARNING: URL looks like Plex DLNA proxy (:8050). "
            "Use native SM6 description.xml for hardware truth.",
            file=sys.stderr,
        )

    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        friendly, avt_control, reciva_url = await _load_endpoints(session, url)
        watcher = Sm6TransportWatcher(
            session,
            description_url=url,
            avt_control_url=avt_control,
            reciva_url=reciva_url,
            friendly_name=friendly,
        )
        print(f"Watching {friendly} @ {url}")
        print(f"AVTransport: {avt_control}")
        print(f"RecivaRadio: {reciva_url}")
        print(f"interval={args.interval}s  (Ctrl+C to stop)\n")

        previous: dict | None = None
        while True:
            row = await watcher.sample()
            changed = previous is None or any(
                row.get(k) != previous.get(k)
                for k in ("transport", "queue_index", "track_id", "uri")
            )
            if changed or not args.changes_only:
                stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                prefix = ">>" if changed and previous is not None else "  "
                print(f"{prefix} {stamp}  {_format_row(row)}", flush=True)
            previous = row
            await asyncio.sleep(args.interval)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Poll SM6 transport state and playlist position (auto-next diagnostics)",
    )
    parser.add_argument("--description-url", default="", help="Native SM6 description.xml URL")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.25,
        help="Poll interval in seconds (default: 0.25)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Print every sample, not only when transport/index/track/uri changes",
    )
    args = parser.parse_args()
    args.changes_only = not args.all
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
