"""SOAP QueueFolder / DeleteAll — Cambridge Stream Magic queue."""
from __future__ import annotations

import re
import xml.sax.saxutils

_UUVOL_CONTROL_NS = "urn:UuVol-com:service:UuVolControl:5"
_PLAYLIST_EXT_NS = "urn:UuVol-com:service:PlaylistExtension:1"

QUEUE_FOLDER_SOAP_ACTION = f'"{_UUVOL_CONTROL_NS}#QueueFolder"'
DELETE_ALL_SOAP_ACTION = f'"{_PLAYLIST_EXT_NS}#DeleteAll"'
SET_SHUFFLE_SOAP_ACTION = f'"{_PLAYLIST_EXT_NS}#SetShuffle"'
SET_REPEAT_SOAP_ACTION = f'"{_PLAYLIST_EXT_NS}#SetRepeat"'
GET_SHUFFLE_SOAP_ACTION = f'"{_PLAYLIST_EXT_NS}#Shuffle"'
GET_REPEAT_SOAP_ACTION = f'"{_PLAYLIST_EXT_NS}#Repeat"'

_A_SHUFFLE = re.compile(r"<aShuffle>([^<]*)</aShuffle>", re.IGNORECASE)
_A_REPEAT = re.compile(r"<aRepeat>([^<]*)</aRepeat>", re.IGNORECASE)

# Plex / HA enqueue → action SM6 QueueFolder
ENQUEUE_TO_SM6_ACTION: dict[str, str] = {
    "replace": "REPLACE",
    "add": "APPEND",
    "next": "PLAY_NEXT",
    "play": "PLAY_NOW",
}


def sm6_action_for_enqueue(enqueue: str) -> str:
    action = ENQUEUE_TO_SM6_ACTION.get(enqueue)
    if action is None:
        raise ValueError(f"unknown enqueue mode: {enqueue}")
    return action


def reciva_radio_invoke_url(description_url: str) -> str:
    base = description_url.rsplit("/", 1)[0]
    return f"{base}/RecivaRadio/invoke"


def uu_playlist_invoke_url(description_url: str) -> str:
    base = description_url.rsplit("/", 1)[0]
    return f"{base}/UuPlaylist/invoke"


def build_queue_folder_body(
    *,
    didl: str,
    action: str,
    server_udn: str,
    navigator_id: str,
) -> str:
    escaped_didl = xml.sax.saxutils.escape(didl)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        f'<u:QueueFolder xmlns:u="{_UUVOL_CONTROL_NS}">'
        f"<DIDL>{escaped_didl}</DIDL>"
        f"<ServerUDN>{xml.sax.saxutils.escape(server_udn)}</ServerUDN>"
        f"<Action>{action}</Action>"
        f"<NavigatorId>{xml.sax.saxutils.escape(navigator_id)}</NavigatorId>"
        "<ExtraInfo></ExtraInfo>"
        "</u:QueueFolder>"
        "</s:Body></s:Envelope>"
    )


def build_delete_all_body() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        f'<u:DeleteAll xmlns:u="{_PLAYLIST_EXT_NS}"/>'
        "</s:Body></s:Envelope>"
    )


def _soap_envelope(action_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<s:Body>{action_xml}</s:Body></s:Envelope>"
    )


def build_set_shuffle_body(enabled: bool) -> str:
    value = "1" if enabled else "0"
    return _soap_envelope(
        f'<u:SetShuffle xmlns:u="{_PLAYLIST_EXT_NS}">'
        f"<aShuffle>{value}</aShuffle>"
        "</u:SetShuffle>"
    )


def build_set_repeat_body(enabled: bool) -> str:
    value = "1" if enabled else "0"
    return _soap_envelope(
        f'<u:SetRepeat xmlns:u="{_PLAYLIST_EXT_NS}">'
        f"<aRepeat>{value}</aRepeat>"
        "</u:SetRepeat>"
    )


def build_get_shuffle_body() -> str:
    return _soap_envelope(f'<u:Shuffle xmlns:u="{_PLAYLIST_EXT_NS}"/>')


def build_get_repeat_body() -> str:
    return _soap_envelope(f'<u:Repeat xmlns:u="{_PLAYLIST_EXT_NS}"/>')


def _parse_bool_flag(xml: str, pattern: re.Pattern[str]) -> bool:
    match = pattern.search(xml)
    if not match:
        return False
    return match.group(1).strip() in {"1", "true", "True", "TRUE"}


def parse_shuffle_response(xml: str) -> bool:
    return _parse_bool_flag(xml, _A_SHUFFLE)


def parse_repeat_response(xml: str) -> bool:
    return _parse_bool_flag(xml, _A_REPEAT)
