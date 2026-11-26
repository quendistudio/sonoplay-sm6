"""Fake Stream Magic 6 at the SOAP HTTP boundary (``g.http.post``)."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from aiohttp import client_reqrep

_ACTION_RE = re.compile(r"<Action>([^<]*)</Action>", re.IGNORECASE)
_KEY_RE = re.compile(r"<Key>([^<]*)</Key>", re.IGNORECASE)
_SOAP_ACTION_RE = re.compile(r"#([A-Za-z0-9_]+)\"?\s*$")


@dataclass
class SoapEvent:
    label: str
    action: str | None = None
    key: str | None = None


class _HttpResponse:
    def __init__(self, text: str, status: int = 200):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@dataclass
class FakeSm6:
    """Simulated SM6: ordered SOAP journal + playlist + injectable faults."""

    journal: list[SoapEvent] = field(default_factory=list)
    playlist: list[str] = field(default_factory=list)
    transport: str = "STOPPED"
    power: str = "ON"
    audio_source: int = 10
    navigator_id: str = "nav-test"
    delay_seconds: float = 0.0
    fail_labels: set[str] = field(default_factory=set)
    timeout_labels: set[str] = field(default_factory=set)
    refuse_labels: set[str] = field(default_factory=set)
    hang_labels: set[str] = field(default_factory=set)
    _hang_release: asyncio.Event | None = field(default=None, repr=False)
    _call_count: dict[str, int] = field(default_factory=dict)

    def labels(self) -> list[str]:
        return [e.label for e in self.journal]

    def release_hang(self) -> None:
        if self._hang_release is not None:
            self._hang_release.set()

    def _label_from_request(self, headers: dict | None, body: str) -> str:
        soap = ""
        if headers:
            soap = str(headers.get("SOAPAction") or headers.get("Soapaction") or "")
        match = _SOAP_ACTION_RE.search(soap)
        op = match.group(1) if match else "Unknown"
        if op == "KeyPressed":
            key_m = _KEY_RE.search(body or "")
            key = key_m.group(1) if key_m else "?"
            return f"KeyPressed {key}"
        if op == "SetPowerState":
            return "SetPowerState ON"
        if op == "SetAudioSourceByNumber":
            return "SetAudioSourceByNumber"
        return op

    async def _apply_faults(self, label: str) -> None:
        base = label.split()[0]
        self._call_count[label] = self._call_count.get(label, 0) + 1
        if label in self.refuse_labels or base in self.refuse_labels:
            key = client_reqrep.ConnectionKey(
                "192.168.50.99", 8050, False, None, None, None, None
            )
            raise aiohttp.ClientConnectorError(key, OSError("refused"))
        if label in self.timeout_labels or base in self.timeout_labels:
            raise asyncio.TimeoutError()
        if label in self.hang_labels or base in self.hang_labels:
            if self._hang_release is None:
                self._hang_release = asyncio.Event()
            await self._hang_release.wait()
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if label in self.fail_labels or base in self.fail_labels:
            raise aiohttp.ClientError(f"{label} failed")

    def _apply_queue_folder(self, action: str | None) -> None:
        if action in {"PLAY_NOW", "REPLACE", "PLAY_FROM_HERE"}:
            if action != "PLAY_FROM_HERE":
                self.playlist = [f"item-{len(self.playlist) + 1}"]
            self.transport = "PLAYING"
        elif action == "APPEND":
            self.playlist.append(f"item-{len(self.playlist) + 1}")
        elif action == "PLAY_NEXT":
            insert_at = 1 if self.playlist else 0
            self.playlist.insert(insert_at, f"item-{len(self.playlist) + 1}")

    def _apply_key(self, key: str | None) -> None:
        if key == "STOP":
            self.transport = "STOPPED"
        elif key == "PLAY_PAUSE":
            self.transport = (
                "PAUSED_PLAYBACK" if self.transport == "PLAYING" else "PLAYING"
            )
        elif key in {"SKIP_NEXT", "SKIP_PREVIOUS"} and self.transport == "STOPPED":
            self.transport = "PLAYING"

    def _response_xml(self, label: str) -> str:
        if label == "QueueFolder":
            return "<Result>OK</Result>"
        if label == "IsRegisteredNavigatorName":
            return (
                f"<IsRegistered>1</IsRegistered>"
                f"<RetNavigatorId>{self.navigator_id}</RetNavigatorId>"
            )
        if label in {"RegisterNamedNavigator", "RegisterNavigator"}:
            return f"<RetNavigatorId>{self.navigator_id}</RetNavigatorId>"
        if label == "GetPowerState":
            return f"<PowerState>{self.power}</PowerState>"
        if label == "GetAudioSourceByNumber":
            return f"<CurrentAudioSource>{self.audio_source}</CurrentAudioSource>"
        if label == "GetPlaylistLength":
            return f"<PlaylistLength>{len(self.playlist)}</PlaylistLength>"
        if label in {"GetMediaQueueIndex", "GetCurrentPlaylistTrack"}:
            return "<Value>0</Value>"
        if label == "GetPlaylistTrackDetails":
            return (
                "<Title>Example Track</Title>"
                "<Artist>Example Artist</Artist>"
                "<Album>Example Album</Album>"
            )
        return "<Result>OK</Result>"

    def post(self, url, data=None, headers=None, timeout=None):
        body = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else (data or "")
        label = self._label_from_request(headers, body)

        async def _run():
            await self._apply_faults(label)
            action = None
            key = None
            if label == "QueueFolder":
                match = _ACTION_RE.search(body)
                action = match.group(1) if match else None
                self._apply_queue_folder(action)
            elif label.startswith("KeyPressed"):
                match = _KEY_RE.search(body)
                key = match.group(1) if match else label.split()[-1]
                self._apply_key(key)
            elif label == "DeleteAll":
                self.playlist.clear()
                self.transport = "STOPPED"
            elif label == "SetPowerState ON":
                self.power = "ON"
            elif label == "SetAudioSourceByNumber":
                self.audio_source = 10
            self.journal.append(SoapEvent(label=label, action=action, key=key))
            return _HttpResponse(self._response_xml(label))

        # aiohttp-style: post() returns async context manager
        class _Ctx:
            def __init__(self, coro):
                self._coro = coro
                self._resp = None

            async def __aenter__(self):
                self._resp = await self._coro
                return self._resp

            async def __aexit__(self, *exc):
                return False

        return _Ctx(_run())

    def install(self, monkeypatch) -> None:
        """Route SOAP HTTP through this fake (keeps Sm6Control._post retry logic)."""
        from utils import g

        monkeypatch.setattr(g, "http", self)

        from dlna.sm6_control import Sm6Control

        async def _nav(self_ctrl):
            return self.navigator_id

        monkeypatch.setattr(Sm6Control, "resolve_plex_navigator_id", _nav)


def fake_dlna_device(
    *,
    uuid: str = "e09a5789-074b-448c-9727-fe268db27f86",
    name: str = "Stream Magic 6",
    location_url: str = "http://192.168.50.99:8050/description.xml",
) -> Any:
    device = MagicMock()
    device.uuid = uuid
    device.name = name
    device.location_url = location_url
    device.manufacturer = "Cambridge Audio"
    device.model_name = "Stream Magic 6"
    device.ip = "192.168.50.99"
    device.get_data = AsyncMock()
    device.volume_max = 100
    device.soap_backoff_remaining = MagicMock(return_value=0.0)
    device.note_soap_success = MagicMock()
    device.note_soap_failure = MagicMock()
    return device
