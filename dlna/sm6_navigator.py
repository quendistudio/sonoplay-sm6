"""Plex navigator registration on SM6 (QueueFolder)."""
from __future__ import annotations

import re
import xml.sax.saxutils

_UUVOL_CONTROL_NS = "urn:UuVol-com:service:UuVolControl:5"

IS_REGISTERED_NAVIGATOR_NAME_ACTION = f'"{_UUVOL_CONTROL_NS}#IsRegisteredNavigatorName"'
REGISTER_NAMED_NAVIGATOR_ACTION = f'"{_UUVOL_CONTROL_NS}#RegisterNamedNavigator"'
REGISTER_NAVIGATOR_ACTION = f'"{_UUVOL_CONTROL_NS}#RegisterNavigator"'
QUEUE_FOLDER_RESULT = re.compile(r"<Result>([^<]*)</Result>", re.IGNORECASE)
IS_REGISTERED = re.compile(r"<IsRegistered>(\d+)</IsRegistered>", re.IGNORECASE)
RET_NAVIGATOR_ID = re.compile(r"<RetNavigatorId>([^<]*)</RetNavigatorId>", re.IGNORECASE)


def _soap_envelope(inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<s:Body>{inner}</s:Body></s:Envelope>"
    )


def build_is_registered_navigator_name_body(name: str) -> str:
    escaped = xml.sax.saxutils.escape(name)
    return _soap_envelope(
        f'<u:IsRegisteredNavigatorName xmlns:u="{_UUVOL_CONTROL_NS}">'
        f"<NavigatorName>{escaped}</NavigatorName>"
        "</u:IsRegisteredNavigatorName>"
    )


def build_register_named_navigator_body(name: str) -> str:
    escaped = xml.sax.saxutils.escape(name)
    return _soap_envelope(
        f'<u:RegisterNamedNavigator xmlns:u="{_UUVOL_CONTROL_NS}">'
        f"<NewNavigatorName>{escaped}</NewNavigatorName>"
        "</u:RegisterNamedNavigator>"
    )


def build_register_navigator_body() -> str:
    return _soap_envelope(f'<u:RegisterNavigator xmlns:u="{_UUVOL_CONTROL_NS}"/>')


def parse_ret_navigator_id(xml: str) -> str | None:
    match = RET_NAVIGATOR_ID.search(xml)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def parse_is_registered_navigator_name(xml: str) -> tuple[bool, str | None]:
    reg = IS_REGISTERED.search(xml)
    nav = parse_ret_navigator_id(xml)
    return (
        reg is not None and reg.group(1).strip() == "1",
        nav,
    )


def parse_queue_folder_result(xml: str) -> str | None:
    match = QUEUE_FOLDER_RESULT.search(xml)
    if not match:
        return None
    return match.group(1).strip() or None
