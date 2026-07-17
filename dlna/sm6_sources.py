"""SOAP sources audio SM6 (UuVolControl:5, RecivaRadio/invoke)."""
from __future__ import annotations

import re

from dlna.sm6_queue import reciva_radio_invoke_url

_UUVOL_CONTROL_NS = "urn:UuVol-com:service:UuVolControl:5"

GET_AUDIO_SOURCES_BY_NUMBER_ACTION = f'"{_UUVOL_CONTROL_NS}#GetAudioSourcesByNumber"'
GET_AUDIO_SOURCE_NAME_ACTION = f'"{_UUVOL_CONTROL_NS}#GetAudioSourceName"'
GET_AUDIO_SOURCE_BY_NUMBER_ACTION = f'"{_UUVOL_CONTROL_NS}#GetAudioSourceByNumber"'
SET_AUDIO_SOURCE_BY_NUMBER_ACTION = f'"{_UUVOL_CONTROL_NS}#SetAudioSourceByNumber"'

_RET_SOURCE_LIST = re.compile(
    r"<RetAudioSourceListValue>([^<]*)</RetAudioSourceListValue>",
    re.IGNORECASE,
)
_RET_SOURCE_NAME = re.compile(
    r"<RetAudioSourceName>([^<]*)</RetAudioSourceName>",
    re.IGNORECASE,
)
_RET_SOURCE_VALUE = re.compile(
    r"<RetAudioSourceValue>([^<]*)</RetAudioSourceValue>",
    re.IGNORECASE,
)

# UPnP push / QueueFolder audio source (SetAudioSourceByNumber id 10).
AUDIO_SOURCE_MEDIA_PLAYER = 10


def build_get_audio_sources_by_number_body() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        f'<u:GetAudioSourcesByNumber xmlns:u="{_UUVOL_CONTROL_NS}"/>'
        "</s:Body></s:Envelope>"
    )


def build_get_audio_source_name_body(source_id: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        f'<u:GetAudioSourceName xmlns:u="{_UUVOL_CONTROL_NS}">'
        f"<InAudioSource>{source_id}</InAudioSource>"
        "</u:GetAudioSourceName>"
        "</s:Body></s:Envelope>"
    )


def build_get_audio_source_by_number_body() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        f'<u:GetAudioSourceByNumber xmlns:u="{_UUVOL_CONTROL_NS}"/>'
        "</s:Body></s:Envelope>"
    )


def build_set_audio_source_by_number_body(source_id: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        f'<u:SetAudioSourceByNumber xmlns:u="{_UUVOL_CONTROL_NS}">'
        f"<NewAudioSourceValue>{source_id}</NewAudioSourceValue>"
        "</u:SetAudioSourceByNumber>"
        "</s:Body></s:Envelope>"
    )


def parse_audio_source_ids(xml: str) -> list[int]:
    match = _RET_SOURCE_LIST.search(xml)
    if not match:
        return []
    raw = match.group(1).strip()
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return ids


def parse_audio_source_name(xml: str) -> str | None:
    match = _RET_SOURCE_NAME.search(xml)
    if not match:
        return None
    name = match.group(1).strip()
    return name or None


def parse_current_audio_source_id(xml: str) -> int | None:
    match = _RET_SOURCE_VALUE.search(xml)
    if not match:
        return None
    return int(match.group(1).strip())
